#!/usr/bin/env python3
"""
hindsight_bot.py — Polymarket bot for "kjøp krypto-favoritten over 93¢, hold til oppgjør"

Regelen er hentet direkte fra Hindsight-backtesten:
    kategori     : crypto
    betingelse   : pris >= 93¢ (første gang markedet oppfyller den)
    tidsvindu    : 6 timer til 7 dager til close
    min. volum   : $1000
    hopp over    : markeder som allerede er "pinned" >= 99¢
    exit         : ingen — hold til oppgjør
    innsats      : flatt beløp per handel

Backtest: 1843 handler, 97.9% treff, +1.6 pp fordel (95% CI ±0.7 naiv, ±0.8 klynget).
Kostnadssveipet er det viktigste tallet i hele filen:

    fordelen blir negativ ved 2.32¢ kostnad
    fordelen er ikke lenger skillbar fra null ved 1.36¢ (klynge-korrigert)

Derfor er denne boten først og fremst en KOSTNADSKONTROLL-maskin, ikke en signalmaskin.
Signalet er trivielt å finne. Fordelen forsvinner hvis du betaler for mye for det.
Konsekvenser for designet:

  * ALDRI market order. Alt går som marketable limit FOK med tak på prisen.
  * Fyllet prises via VWAP gjennom ordreboken, ikke via beste ask.
  * Hver handel logger oppnådd kostnad (fyll − midtpunkt) slik at du kan måle
    din egen faktiske kostnad mot 1.36¢-grensen før du risikerer penger.
  * Klyngekontroll: backtestens 1843 handler var bare ~33 uavhengige veddemål.
    Boten begrenser derfor eksponering per oppgjørsdag og per underliggende.

Kjør i DRY_RUN til du har minst 100 skygge-handler med målt kostnad. Se README.md.

SDK: py-clob-client-v2 (CLOB V2). pip install py-clob-client-v2 requests
Merk: Polymarket anbefaler nå Polymarket/py-sdk for nye prosjekter — API-flatene
under er isolert i PolymarketExecution slik at bytte er en liten jobb.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

# ─────────────────────────────────────────────────────────────────────────────
# KONFIGURASJON
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Config:
    # -- regelen fra backtesten -------------------------------------------------
    entry_min: float = 0.93          # kjøp når prisen er >= denne
    entry_max: float = 0.99          # hopp over pinnede markeder (>= 99¢)
    min_hours_to_close: float = 6.0
    max_days_to_close: float = 7.0
    min_volume_usd: float = 1000.0

    # -- kostnadskontroll (det som avgjør om fordelen overlever) ----------------
    cost_budget_cents: float = 0.5   # backtestens antakelse
    cost_hard_cap_cents: float = 1.0 # avbryt handelen over dette, uansett
    max_effective_price: float = 0.99  # VWAP-fyll må ligge under dette

    # -- posisjonsstørrelse ----------------------------------------------------
    stake_usd: float = 25.0          # start lavt. Backtesten brukte flate $100.
    min_shares: int = 5              # Polymarkets minimum
    require_depth_multiple: float = 1.0  # boken må kunne fylle hele innsatsen

    # -- klyngekontroll --------------------------------------------------------
    # 1843 handler ble til 33 klynger. To BTC-markeder som gjøres opp samme dag
    # er ett markedsutslag talt to ganger, ikke to uavhengige tester.
    max_positions_per_resolution_day: int = 3
    max_positions_per_asset_per_day: int = 1
    max_open_positions: int = 20
    max_open_notional_usd: float = 500.0
    max_new_positions_per_day: int = 6

    # -- stoppkriterier (avtal dem på forhånd, ikke endre dem underveis) --------
    stop_on_consecutive_losses: int = 3      # backtestens lengste tapsrekke var 2
    stop_on_drawdown_usd: float = 200.0      # ~8 innsatser ved $25
    stop_on_losses_in_first_n: tuple = (5, 60)  # 5 tap på de første 60 handlene

    # -- drift -----------------------------------------------------------------
    dry_run: bool = True
    poll_seconds: int = 300
    max_markets_scanned: int = 2000  # Gamma nekter paginering forbi ~2000
    gamma_page_size: int = 100       # Gamma gir maks 100 per side
    assets: tuple = ("BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX")
    state_path: str = "state.json"
    trade_log_path: str = "trades.csv"
    gamma_host: str = "https://gamma-api.polymarket.com"
    clob_host: str = "https://clob.polymarket.com"
    chain_id: int = 137


CFG = Config()

log = logging.getLogger("hindsight")


# ─────────────────────────────────────────────────────────────────────────────
# HJELPERE
# ─────────────────────────────────────────────────────────────────────────────

ASSET_PATTERNS = {
    "BTC": r"\b(bitcoin|btc)\b",
    "ETH": r"\b(ethereum|ether|eth)\b",
    "SOL": r"\b(solana|sol)\b",
    "XRP": r"\b(xrp|ripple)\b",
    "DOGE": r"\b(dogecoin|doge)\b",
    "ADA": r"\b(cardano|ada)\b",
    "LINK": r"\b(chainlink|link)\b",
    "AVAX": r"\b(avalanche|avax)\b",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def detect_asset(question: str, cfg: Config = CFG) -> str | None:
    q = question.lower()
    for asset in cfg.assets:
        pat = ASSET_PATTERNS.get(asset)
        if pat and re.search(pat, q):
            return asset
    return None


def json_field(value: Any) -> Any:
    """Gamma returnerer flere felt som JSON-strenger ('["Yes","No"]')."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def round_to_tick(price: float, tick: float, up: bool = True) -> float:
    steps = price / tick
    steps = int(steps) + 1 if up and steps != int(steps) else int(round(steps))
    return round(steps * tick, 6)


# ─────────────────────────────────────────────────────────────────────────────
# MARKEDSSØK (Gamma)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Candidate:
    market_id: str
    question: str
    slug: str
    condition_id: str
    token_id: str
    outcome: str
    asset: str
    end_date: datetime
    volume: float
    tick_size: float
    neg_risk: bool

    @property
    def resolution_day(self) -> str:
        return self.end_date.date().isoformat()

    @property
    def cluster_key(self) -> str:
        return f"{self.resolution_day}:{self.asset}"


class GammaClient:
    def __init__(self, cfg: Config = CFG):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "hindsight-bot/1.0"})

    def open_markets(self) -> list[dict]:
        """Hent åpne markeder som lukker innenfor tidsvinduet.

        Datoene sendes med 'Z'-suffiks, ikke '+00:00' — plusstegnet blir tolket
        som mellomrom i en URL og gjør filteret meningsløst uten at Gamma klager.
        Alle filtre gjentas lokalt uansett, så en stille serverfeil blir synlig
        i trakten i stedet for å gi oss feil markeder.
        """
        cfg = self.cfg
        stamp = "%Y-%m-%dT%H:%M:%SZ"
        lo = (now_utc() + timedelta(hours=cfg.min_hours_to_close)).strftime(stamp)
        hi = (now_utc() + timedelta(days=cfg.max_days_to_close)).strftime(stamp)

        out, offset = [], 0
        while offset < cfg.max_markets_scanned:
            params = {
                "closed": "false",
                "active": "true",
                "limit": cfg.gamma_page_size,
                "offset": offset,
                "order": "endDate",
                "ascending": "true",
                "end_date_min": lo,
                "end_date_max": hi,
            }
            try:
                r = self.s.get(f"{cfg.gamma_host}/markets", params=params, timeout=30)
                r.raise_for_status()
                batch = r.json()
            except requests.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else "?"
                log.warning("Gamma stoppet paginering ved offset %d (HTTP %s)", offset, code)
                break
            except (requests.RequestException, ValueError) as exc:
                log.warning("Gamma-forespørsel feilet ved offset %d: %s", offset, exc)
                break

            if not batch:
                break
            out.extend(batch)
            if len(batch) < cfg.gamma_page_size:
                break
            offset += cfg.gamma_page_size

        log.info("Gamma: %d markeder hentet (vindu %s → %s)", len(out), lo, hi)
        return out

    def candidates(self) -> list[Candidate]:
        cands: list[Candidate] = []
        f = {"rå": 0, "åpen": 0, "krypto": 0, "tidsvindu": 0, "volum": 0, "tokens": 0}
        samples: list[str] = []
        for m in self.open_markets():
            f["rå"] += 1
            if m.get("closed") or not m.get("active"):
                continue
            if m.get("acceptingOrders") is False:
                continue
            f["åpen"] += 1

            question = m.get("question") or ""
            if len(samples) < 8:
                samples.append(question)
            asset = detect_asset(question)
            if not asset:
                continue  # ikke krypto — utenfor regelen
            f["krypto"] += 1

            end = parse_iso(m.get("endDate") or "")
            if not end:
                continue
            hours_left = (end - now_utc()).total_seconds() / 3600
            if not (self.cfg.min_hours_to_close <= hours_left <= self.cfg.max_days_to_close * 24):
                continue
            f["tidsvindu"] += 1

            volume = float(m.get("volumeNum") or m.get("volume") or 0)
            if volume < self.cfg.min_volume_usd:
                continue
            f["volum"] += 1

            tokens = json_field(m.get("clobTokenIds")) or []
            outcomes = json_field(m.get("outcomes")) or []
            if len(tokens) != len(outcomes) or not tokens:
                continue
            tick = float(m.get("orderPriceMinTickSize") or 0.01)
            f["tokens"] += 1

            for token_id, outcome in zip(tokens, outcomes):
                cands.append(
                    Candidate(
                        market_id=str(m.get("id")),
                        question=question,
                        slug=m.get("slug") or "",
                        condition_id=m.get("conditionId") or "",
                        token_id=str(token_id),
                        outcome=str(outcome),
                        asset=asset,
                        end_date=end,
                        volume=volume,
                        tick_size=tick,
                        neg_risk=bool(m.get("negRisk")),
                    )
                )
        log.info(
            "Trakt: %d rå → %d åpne → %d krypto → %d i tidsvindu → %d over volum → "
            "%d markeder → %d utfall",
            f["rå"], f["åpen"], f["krypto"], f["tidsvindu"], f["volum"], f["tokens"], len(cands),
        )
        if f["krypto"] == 0 and samples:
            log.info("Ingen krypto funnet. Eksempler på titler som kom inn:")
            for s in samples:
                log.info("   · %s", s[:90])
        return cands


# ─────────────────────────────────────────────────────────────────────────────
# ORDREBOK OG PRISING
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Quote:
    best_ask: float
    midpoint: float
    vwap: float           # snittpris for hele innsatsen
    fillable_usd: float
    cost_cents: float     # vwap − midtpunkt, i cent
    shares: float


def walk_asks(asks: list[tuple[float, float]], notional: float) -> tuple[float, float, float]:
    """Gå gjennom ask-siden og regn ut snittpris for gitt beløp.

    Returnerer (vwap, fylt_beløp, antall_andeler).
    """
    spent = 0.0
    shares = 0.0
    for price, size in sorted(asks):
        level_value = price * size
        take = min(level_value, notional - spent)
        if take <= 0:
            break
        spent += take
        shares += take / price
        if spent >= notional - 1e-9:
            break
    vwap = spent / shares if shares else 0.0
    return vwap, spent, shares


class PolymarketExecution:
    """Tynt lag rundt CLOB-klienten. All SDK-avhengighet ligger her."""

    def __init__(self, cfg: Config = CFG):
        self.cfg = cfg
        self.client = None
        self._connect()

    def _connect(self) -> None:
        try:
            from py_clob_client_v2 import ClobClient  # type: ignore
        except ImportError:
            log.error("py-clob-client-v2 mangler: pip install py-clob-client-v2")
            raise

        pk = os.environ.get("POLYMARKET_PK")
        if self.cfg.dry_run or not pk:
            # Lesetilgang holder for skyggekjøring.
            self.client = ClobClient(host=self.cfg.clob_host, chain_id=self.cfg.chain_id)
            log.info("CLOB tilkoblet i lesemodus (ingen signering).")
            return

        client = ClobClient(host=self.cfg.clob_host, chain_id=self.cfg.chain_id, key=pk)
        creds = client.create_or_derive_api_key()
        self.client = ClobClient(
            host=self.cfg.clob_host, chain_id=self.cfg.chain_id, key=pk, creds=creds
        )
        log.info("CLOB tilkoblet med signering. LIVE-modus.")

    # -- lesing ---------------------------------------------------------------

    def quote(self, token_id: str, notional: float) -> Quote | None:
        try:
            book = self.client.get_order_book(token_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("ordrebok feilet for %s: %s", token_id, exc)
            return None

        asks = [(float(l.price), float(l.size)) for l in (getattr(book, "asks", None) or [])]
        bids = [(float(l.price), float(l.size)) for l in (getattr(book, "bids", None) or [])]
        if not asks or not bids:
            return None

        best_ask = min(p for p, _ in asks)
        best_bid = max(p for p, _ in bids)
        midpoint = (best_ask + best_bid) / 2

        vwap, filled, shares = walk_asks(asks, notional)
        if shares <= 0:
            return None

        return Quote(
            best_ask=best_ask,
            midpoint=midpoint,
            vwap=vwap,
            fillable_usd=filled,
            cost_cents=(vwap - midpoint) * 100,
            shares=shares,
        )

    # -- handel ---------------------------------------------------------------

    def buy_fok(self, cand: Candidate, limit_price: float, shares: float) -> dict:
        """Marketable limit, fill-or-kill. Aldri market order — prisen er fordelen."""
        if self.cfg.dry_run:
            return {"status": "dry_run", "price": limit_price, "size": shares}

        from py_clob_client_v2 import (  # type: ignore
            OrderArgs,
            OrderType,
            PartialCreateOrderOptions,
            Side,
        )

        resp = self.client.create_and_post_order(
            order_args=OrderArgs(
                token_id=cand.token_id,
                price=limit_price,
                side=Side.BUY,
                size=round(shares, 2),
            ),
            options=PartialCreateOrderOptions(tick_size=str(cand.tick_size)),
            order_type=OrderType.FOK,
        )
        return resp if isinstance(resp, dict) else {"raw": str(resp)}


# ─────────────────────────────────────────────────────────────────────────────
# TILSTAND OG RISIKO
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Position:
    token_id: str
    market_id: str
    question: str
    asset: str
    outcome: str
    resolution_day: str
    entered_at: str
    price: float
    midpoint: float
    cost_cents: float
    shares: float
    notional: float
    dry_run: bool
    resolved: bool = False
    won: bool | None = None
    pnl: float = 0.0


class State:
    def __init__(self, cfg: Config = CFG):
        self.cfg = cfg
        self.path = Path(cfg.state_path)
        self.positions: list[Position] = []
        self.halted: bool = False
        self.halt_reason: str = ""
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text())
        self.positions = [Position(**p) for p in raw.get("positions", [])]
        self.halted = raw.get("halted", False)
        self.halt_reason = raw.get("halt_reason", "")

    def save(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "positions": [asdict(p) for p in self.positions],
                    "halted": self.halted,
                    "halt_reason": self.halt_reason,
                    "updated": now_utc().isoformat(),
                },
                indent=2,
            )
        )

    # -- oppslag --------------------------------------------------------------

    def has_token(self, token_id: str) -> bool:
        return any(p.token_id == token_id for p in self.positions)

    def has_market(self, market_id: str) -> bool:
        return any(p.market_id == market_id for p in self.positions)

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions if not p.resolved]

    def count_cluster(self, day: str) -> int:
        return sum(1 for p in self.positions if p.resolution_day == day and not p.resolved)

    def count_asset_day(self, day: str, asset: str) -> int:
        return sum(
            1
            for p in self.positions
            if p.resolution_day == day and p.asset == asset and not p.resolved
        )

    def entered_today(self) -> int:
        today = now_utc().date().isoformat()
        return sum(1 for p in self.positions if p.entered_at[:10] == today)

    def open_notional(self) -> float:
        return sum(p.notional for p in self.open_positions())

    # -- stoppkriterier -------------------------------------------------------

    def check_stops(self) -> str | None:
        cfg = self.cfg
        settled = [p for p in self.positions if p.resolved]

        streak = 0
        for p in reversed(settled):
            if p.won is False:
                streak += 1
            else:
                break
        if streak >= cfg.stop_on_consecutive_losses:
            return f"{streak} tap på rad (backtestens verste var 2)"

        equity, peak, dd = 0.0, 0.0, 0.0
        for p in settled:
            equity += p.pnl
            peak = max(peak, equity)
            dd = max(dd, peak - equity)
        if dd >= cfg.stop_on_drawdown_usd:
            return f"drawdown ${dd:.0f} over grensen ${cfg.stop_on_drawdown_usd:.0f}"

        n_losses, n_first = cfg.stop_on_losses_in_first_n
        if len(settled) <= n_first:
            losses = sum(1 for p in settled if p.won is False)
            if losses >= n_losses:
                return f"{losses} tap på de første {len(settled)} handlene"
        return None

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason
        self.save()
        log.error("STOPP: %s. Boten handler ikke mer før state.json nullstilles.", reason)


# ─────────────────────────────────────────────────────────────────────────────
# FILTER
# ─────────────────────────────────────────────────────────────────────────────


def passes_rules(cand: Candidate, quote: Quote, state: State, cfg: Config = CFG) -> str | None:
    """Returnerer None hvis handelen er godkjent, ellers grunnen til at den avvises."""
    if state.has_market(cand.market_id):
        return "allerede i markedet"
    if quote.best_ask < cfg.entry_min:
        return f"pris {quote.best_ask:.3f} under {cfg.entry_min}"
    if quote.best_ask >= cfg.entry_max:
        return f"pris {quote.best_ask:.3f} pinnet >= {cfg.entry_max}"
    if quote.vwap >= cfg.max_effective_price:
        return f"vwap {quote.vwap:.4f} over taket"
    if quote.cost_cents > cfg.cost_hard_cap_cents:
        return f"kostnad {quote.cost_cents:.2f}¢ over hardt tak"
    if quote.fillable_usd < cfg.stake_usd * cfg.require_depth_multiple - 0.01:
        return f"for tynn bok (kun ${quote.fillable_usd:.0f})"
    if quote.shares < cfg.min_shares:
        return f"{quote.shares:.1f} andeler under minimum {cfg.min_shares}"

    day = cand.resolution_day
    if state.count_cluster(day) >= cfg.max_positions_per_resolution_day:
        return f"klyngetak for {day} nådd"
    if state.count_asset_day(day, cand.asset) >= cfg.max_positions_per_asset_per_day:
        return f"{cand.asset} allerede tatt for {day}"
    if len(state.open_positions()) >= cfg.max_open_positions:
        return "for mange åpne posisjoner"
    if state.open_notional() + cfg.stake_usd > cfg.max_open_notional_usd:
        return "kapitaltak nådd"
    if state.entered_today() >= cfg.max_new_positions_per_day:
        return "dagens inngangstak nådd"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# LOGGING AV HANDLER
# ─────────────────────────────────────────────────────────────────────────────

TRADE_FIELDS = [
    "timestamp", "mode", "asset", "outcome", "question", "resolution_day",
    "best_ask", "midpoint", "vwap", "cost_cents", "shares", "notional", "market_id",
]


def log_trade(pos: Position, cfg: Config = CFG) -> None:
    path = Path(cfg.trade_log_path)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
        if new:
            w.writeheader()
        w.writerow({
            "timestamp": pos.entered_at,
            "mode": "dry" if pos.dry_run else "live",
            "asset": pos.asset,
            "outcome": pos.outcome,
            "question": pos.question,
            "resolution_day": pos.resolution_day,
            "best_ask": f"{pos.price:.4f}",
            "midpoint": f"{pos.midpoint:.4f}",
            "vwap": f"{pos.price:.4f}",
            "cost_cents": f"{pos.cost_cents:.3f}",
            "shares": f"{pos.shares:.2f}",
            "notional": f"{pos.notional:.2f}",
            "market_id": pos.market_id,
        })


def notify(msg: str) -> None:
    """Valgfri Telegram-varsling. Sett TG_TOKEN og TG_CHAT for å slå den på."""
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if not token or not chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": msg},
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        log.debug("telegram-varsel feilet")


# ─────────────────────────────────────────────────────────────────────────────
# OPPGJØR
# ─────────────────────────────────────────────────────────────────────────────


def settle_positions(state: State, gamma: GammaClient, cfg: Config = CFG) -> None:
    """Sjekk om åpne posisjoner er gjort opp, og bokfør resultatet."""
    for pos in state.open_positions():
        if parse_iso(pos.entered_at) and (now_utc() - parse_iso(pos.entered_at)).days > 30:
            continue
        try:
            r = requests.get(f"{cfg.gamma_host}/markets/{pos.market_id}", timeout=20)
            r.raise_for_status()
            m = r.json()
        except Exception:  # noqa: BLE001
            continue
        if not m.get("closed"):
            continue

        prices = json_field(m.get("outcomePrices")) or []
        outcomes = json_field(m.get("outcomes")) or []
        payout = None
        for name, price in zip(outcomes, prices):
            if str(name) == pos.outcome:
                payout = float(price)
        if payout is None:
            continue
        if payout not in (0.0, 1.0):
            continue  # streng settlement — hopp over uavklarte

        pos.resolved = True
        pos.won = payout == 1.0
        pos.pnl = pos.shares * payout - pos.notional
        log.info("Oppgjør: %s → %s (%+.2f)", pos.question[:60],
                 "vinn" if pos.won else "tap", pos.pnl)
    state.save()


# ─────────────────────────────────────────────────────────────────────────────
# HOVEDLØKKE
# ─────────────────────────────────────────────────────────────────────────────


def scan_once(cfg: Config, state: State, gamma: GammaClient, exe: PolymarketExecution) -> None:
    if state.halted:
        log.warning("Boten er stoppet: %s", state.halt_reason)
        return

    settle_positions(state, gamma, cfg)

    stop = state.check_stops()
    if stop:
        state.halt(stop)
        notify(f"Hindsight-bot stoppet: {stop}")
        return

    cands = gamma.candidates()
    log.info("%d kandidater innenfor vindu/volum", len(cands))

    taken = 0
    skips: dict[str, int] = {}
    in_band = 0

    def note(reason: str) -> None:
        key = re.sub(r"[\d.,]+", "N", reason)  # slå sammen like grunner med ulike tall
        skips[key] = skips.get(key, 0) + 1

    for cand in cands:
        if state.has_market(cand.market_id):
            note("allerede i markedet")
            continue
        quote = exe.quote(cand.token_id, cfg.stake_usd)
        if not quote:
            note("ingen ordrebok")
            continue
        if quote.best_ask < cfg.entry_min:
            note("pris under 93¢")
            continue
        if quote.best_ask >= cfg.entry_max:
            note("pinnet 99¢ eller over")
            continue
        in_band += 1

        reason = passes_rules(cand, quote, state, cfg)
        if reason:
            note(reason)
            log.info("hopper over %s (%.3f): %s", cand.question[:50], quote.best_ask, reason)
            continue

        limit = min(round_to_tick(quote.vwap, cand.tick_size, up=True), 0.999)
        shares = cfg.stake_usd / limit

        if quote.cost_cents > cfg.cost_budget_cents:
            log.info("kostnad %.2f¢ over budsjett (%.2f¢) men under hardt tak — tar den",
                     quote.cost_cents, cfg.cost_budget_cents)

        resp = exe.buy_fok(cand, limit, shares)
        status = str(resp.get("status", resp))
        log.info("KJØP %s %s @ %.4f (mid %.4f, kostnad %.2f¢) → %s",
                 cand.asset, cand.outcome, limit, quote.midpoint, quote.cost_cents, status)

        pos = Position(
            token_id=cand.token_id,
            market_id=cand.market_id,
            question=cand.question,
            asset=cand.asset,
            outcome=cand.outcome,
            resolution_day=cand.resolution_day,
            entered_at=now_utc().isoformat(),
            price=limit,
            midpoint=quote.midpoint,
            cost_cents=quote.cost_cents,
            shares=shares,
            notional=cfg.stake_usd,
            dry_run=cfg.dry_run,
        )
        state.positions.append(pos)
        log_trade(pos, cfg)
        state.save()
        taken += 1
        notify(f"{'[TØRR] ' if cfg.dry_run else ''}Kjøpt {cand.asset} {cand.outcome} "
               f"@ {limit:.3f} — {cand.question[:70]}")

        if state.entered_today() >= cfg.max_new_positions_per_day:
            break

    log.info("Runde ferdig: %d nye posisjoner, %d åpne · %d utfall lå i 93–99¢-båndet",
             taken, len(state.open_positions()), in_band)
    if skips:
        log.info("Forkastet fordi:")
        for reason, n in sorted(skips.items(), key=lambda kv: -kv[1]):
            log.info("   %4d × %s", n, reason)


def report(state: State) -> None:
    settled = [p for p in state.positions if p.resolved]
    open_pos = state.open_positions()
    print(f"\nÅpne posisjoner : {len(open_pos)}  (${state.open_notional():.0f} bundet)")
    print(f"Gjort opp       : {len(settled)}")
    if settled:
        wins = sum(1 for p in settled if p.won)
        pnl = sum(p.pnl for p in settled)
        wr = wins / len(settled) * 100
        avg_entry = sum(p.price for p in settled) / len(settled)
        edge = wr - avg_entry * 100
        print(f"Treffrate       : {wr:.1f}%  ({wins}/{len(settled)})")
        print(f"Snitt inngang   : {avg_entry*100:.1f}¢")
        print(f"Fordel          : {edge:+.2f} pp   (backtest: +1.6 pp)")
        print(f"P&L             : ${pnl:+.2f}")
    all_costs = [p.cost_cents for p in state.positions]
    if all_costs:
        avg_cost = sum(all_costs) / len(all_costs)
        worst = max(all_costs)
        print(f"\nMålt kostnad    : snitt {avg_cost:.2f}¢, verste {worst:.2f}¢")
        print(f"Fordelen dør ved: 2.32¢   ·  blir støy ved 1.36¢ (klynget)")
        if avg_cost > 1.36:
            print("→ Din faktiske kostnad spiser opp fordelen. Ikke gå live.")
        elif avg_cost > 0.5:
            print("→ Over backtestens antakelse. Forvent lavere fordel enn +1.6 pp.")
        else:
            print("→ Innenfor backtestens antakelse.")
    if state.halted:
        print(f"\nSTOPPET: {state.halt_reason}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Hindsight Polymarket-bot")
    ap.add_argument("--live", action="store_true", help="handle med ekte penger")
    ap.add_argument("--once", action="store_true", help="kjør én runde og avslutt")
    ap.add_argument("--report", action="store_true", help="vis status og avslutt")
    ap.add_argument("--stake", type=float, help="overstyr innsats per handel")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = CFG
    cfg.dry_run = not args.live
    if args.stake:
        cfg.stake_usd = args.stake

    state = State(cfg)

    if args.report:
        report(state)
        return 0

    if args.live:
        if not os.environ.get("POLYMARKET_PK"):
            log.error("POLYMARKET_PK mangler i miljøet.")
            return 1
        confirmed = input(
            f"LIVE-modus, ${cfg.stake_usd:.0f} per handel. "
            f"Har du minst 100 skygge-handler med snittkostnad under 1.36¢? (ja/nei) "
        )
        if confirmed.strip().lower() not in ("ja", "yes", "y"):
            log.info("Avbrutt.")
            return 0

    gamma = GammaClient(cfg)
    exe = PolymarketExecution(cfg)

    if args.once:
        scan_once(cfg, state, gamma, exe)
        report(state)
        return 0

    log.info("Starter løkke (%s), poll hvert %ds",
             "TØRRKJØRING" if cfg.dry_run else "LIVE", cfg.poll_seconds)
    while True:
        try:
            scan_once(cfg, state, gamma, exe)
        except KeyboardInterrupt:
            log.info("Avslutter.")
            return 0
        except Exception as exc:  # noqa: BLE001
            log.exception("runde feilet: %s", exc)
        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
