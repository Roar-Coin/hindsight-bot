#!/usr/bin/env python3
"""
booklogger.py — tørrkjøring av værregelen på Polymarket.

Måler to av de tre tallene uten å plassere en eneste ordre:
  1. Faktisk gjennomføringskostnad — avviket mellom prisen regelen SÅ og
     prisen $100 faktisk ville blitt fylt til, ved å gå gjennom ask-siden.
  2. Signaler som aldri ble til en posisjon — krysninger der boken var tom
     eller for tynn til å ta $100.

Det tredje tallet (klynger per døgn) faller ut av `report`-kommandoen.

Ingen kapital, ingen bot-plattform, ingen PolyCop-avhengighet.

Bruk:
    python booklogger.py watch            # loop, logger til vaer-book.jsonl
    python booklogger.py watch --once     # én runde (for cron/Actions)
    python booklogger.py report           # les loggen, skriv ut tallene

MERK — dette er skrevet uten nett tilgjengelig, så endepunktene under er
ikke kjørt mot ekte API. Sjekk de tre merket VERIFISER mot ingest.py før
første kjøring.
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ── Parametre — skal matche backtesten ────────────────────────────────────
THRESHOLD = 0.80        # kjøp første gang prisen krysser denne
MAX_ENTRY = 0.99        # gapvakt: appen blokkerer fyll >= 99,9¢
MIN_VOLUME = 250.0      # samme gulv som backtesten
STAKE = 100.0           # flat innsats per handel
POLL_SEC = 120          # værbøker beveger seg sakte; 2 min er rikelig
REQ_PAUSE = 0.15        # pause mellom CLOB-kall

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

LOG = "vaer-book.jsonl"
STATE = "vaer-state.json"

WEATHER_WORDS = (
    "temperature", "temp ", "rain", "rainfall", "snow", "snowfall",
    "hurricane", "storm", "heat", "degrees", "weather", "precipitation",
)


# ── HTTP ──────────────────────────────────────────────────────────────────
def get(url, params=None, tries=3):
    if params:
        url = f"{url}?{urlencode(params)}"
    for n in range(tries):
        try:
            req = Request(url, headers={"User-Agent": "booklogger/1"})
            with urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if n == tries - 1:
                print(f"  ! {url[:70]} -> {e}", file=sys.stderr)
                return None
            time.sleep(1.5 * (n + 1))


# ── Markedsutvalg ─────────────────────────────────────────────────────────
def is_weather(m):
    """VERIFISER: bytt ut med TAG_MAP-oppslaget fra ingest.py hvis du har det.
    Husk include_tag=true — uten den returnerer Gamma ingen tags."""
    tags = m.get("tags") or []
    for t in tags:
        label = (t.get("label") or t.get("slug") or "").lower() if isinstance(t, dict) else str(t).lower()
        if "weather" in label or "climate" in label:
            return True
    q = (m.get("question") or "").lower()
    return any(w in q for w in WEATHER_WORDS)


def active_weather_markets():
    """Aktive, uavgjorte værmarkeder over volumgulvet."""
    out, offset = [], 0
    while True:
        # VERIFISER paginering/feltnavn mot ingest.py
        page = get(f"{GAMMA}/markets", {
            "closed": "false",
            "active": "true",
            "include_tag": "true",
            "limit": 500,
            "offset": offset,
        })
        if not page:
            break
        for m in page:
            if float(m.get("volumeNum") or m.get("volume") or 0) < MIN_VOLUME:
                continue
            if is_weather(m):
                out.append(m)
        if len(page) < 500:
            break
        offset += 500
    return out


def token_ids(m):
    """clobTokenIds er ofte en JSON-streng, ikke en liste."""
    raw = m.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    return list(raw or [])


def outcomes(m):
    raw = m.get("outcomes")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    return list(raw or [])


# ── Pris og bok ───────────────────────────────────────────────────────────
def signal_prices(tid):
    """Tre referanser. Backtesten leste handelsprisserien, så `last` er den
    som ligner mest — men logg alle tre, da slipper vi å gjette."""
    last = get(f"{CLOB}/last-trade-price", {"token_id": tid})
    mid = get(f"{CLOB}/midpoint", {"token_id": tid})
    f = lambda d, k="price": float(d[k]) if d and k in d else None
    return f(last), f(mid, "mid") if mid and "mid" in mid else f(mid)


def walk_asks(asks, stake=STAKE):
    """Går gjennom ask-siden til $100 er brukt opp.
    Returnerer VWAP, antall aksjer, hvor mange nivåer vi spiste,
    om vi ble fylt helt, og hvor mye boken faktisk kunne ta."""
    spent = shares = 0.0
    levels = 0
    for lvl in sorted(asks, key=lambda a: float(a["price"])):
        p, sz = float(lvl["price"]), float(lvl["size"])
        if p > MAX_ENTRY:
            break
        room = stake - spent
        if room <= 1e-9:
            break
        take = min(sz, room / p)
        if take <= 0:
            break
        spent += take * p
        shares += take
        levels += 1
    vwap = spent / shares if shares else None
    return vwap, shares, levels, spent >= stake - 1e-6, spent


# ── Tilstand ──────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {"seen": {}}


def save_state(s):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, STATE)


def append(rec):
    with open(LOG, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── Én runde ──────────────────────────────────────────────────────────────
def sweep(state):
    markets = active_weather_markets()
    print(f"[{datetime.now(timezone.utc):%H:%M}] {len(markets)} værmarkeder over ${MIN_VOLUME:.0f}",
          file=sys.stderr)
    hits = 0

    for m in markets:
        tids, outs = token_ids(m), outcomes(m)
        for i, tid in enumerate(tids):
            if tid in state["seen"]:
                continue          # regelen kjøper FØRSTE krysning, aldri igjen

            last, mid = signal_prices(tid)
            time.sleep(REQ_PAUSE)
            sig = last if last is not None else mid
            if sig is None or sig < THRESHOLD:
                continue

            book = get(f"{CLOB}/book", {"token_id": tid})
            time.sleep(REQ_PAUSE)
            asks = (book or {}).get("asks") or []
            vwap, shares, levels, filled, avail = walk_asks(asks)
            best_ask = min((float(a["price"]) for a in asks), default=None)

            status = "filled" if filled else ("partial" if shares else "empty")
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "token_id": tid,
                "condition_id": m.get("conditionId"),
                "slug": m.get("slug"),
                "event_slug": (m.get("events") or [{}])[0].get("slug") if m.get("events") else None,
                "question": m.get("question"),
                "outcome": outs[i] if i < len(outs) else None,
                "end_date": m.get("endDate"),
                "volume": float(m.get("volumeNum") or m.get("volume") or 0),
                "sig_last": last,
                "sig_mid": mid,
                "best_ask": best_ask,
                "fill_vwap": vwap,
                "shares": round(shares, 2),
                "levels": levels,
                "filled": filled,
                "notional_available": round(avail, 2),
                "status": status,
                # kostnaden, i cent — dette er tallet hele øvelsen handler om
                "slip_c": round((vwap - sig) * 100, 3) if vwap else None,
                "slip_vs_mid_c": round((vwap - mid) * 100, 3) if vwap and mid else None,
            }
            append(rec)
            state["seen"][tid] = rec["ts"]
            hits += 1
            print(f"  + {status:7} {rec['slip_c']}¢  {(rec['question'] or '')[:52]}",
                  file=sys.stderr)

    if hits:
        save_state(state)
    return hits


# ── Rapport ───────────────────────────────────────────────────────────────
def cluster_key(r):
    """Klynge = emne + oppgjørsdag. Emnet tas fra event-slug når den finnes,
    ellers spørsmålet med tall strippet ut."""
    day = (r.get("end_date") or "")[:10]
    subj = r.get("event_slug")
    if not subj:
        subj = "".join(c for c in (r.get("question") or "") if not c.isdigit())[:60]
    return (subj, day)


def report():
    if not os.path.exists(LOG):
        print("Ingen logg ennå.")
        return
    rows = [json.loads(l) for l in open(LOG) if l.strip()]
    if not rows:
        print("Tom logg.")
        return

    filled = [r for r in rows if r["status"] == "filled"]
    slips = sorted(r["slip_c"] for r in filled if r["slip_c"] is not None)
    n_bad = sum(1 for r in rows if r["status"] != "filled")

    clusters = defaultdict(list)
    for r in rows:
        clusters[cluster_key(r)].append(r)
    days = {r["ts"][:10] for r in rows}

    print(f"\nSignaler          {len(rows)}")
    print(f"  fylt            {len(filled)}")
    print(f"  ufyllbare       {n_bad}  ({n_bad / len(rows):.0%})   [stopp ved 33 %]")
    if slips:
        avg = sum(slips) / len(slips)
        p95 = slips[min(len(slips) - 1, int(0.95 * len(slips)))]
        print(f"\nGjennomføringskostnad, fylte handler")
        print(f"  snitt           {avg:.2f}¢")
        print(f"  median          {slips[len(slips) // 2]:.2f}¢")
        print(f"  p95             {p95:.2f}¢")
        print(f"  verste          {slips[-1]:.2f}¢")
        print(f"  backtest antok  0,50¢")
        print(f"  tak (fordel +1,3 pp)   1,41¢  {'OK' if avg < 1.41 else 'BRUDD'}")
        print(f"  tak (fordel +0,7 pp)   1,00¢  {'OK' if avg < 1.00 else 'BRUDD'}")
    print(f"\nKlynger           {len(clusters)} over {len(days)} døgn"
          f"  ({len(clusters) / max(len(days), 1):.1f} per døgn)")
    print(f"  handler/klynge  {len(rows) / max(len(clusters), 1):.1f}\n")


# ── main ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["watch", "report"])
    ap.add_argument("--once", action="store_true", help="én runde, så avslutt")
    ap.add_argument("--minutes", type=int, default=0, help="stopp etter N minutter")
    a = ap.parse_args()

    if a.cmd == "report":
        return report()

    state = load_state()
    t0 = time.time()
    while True:
        try:
            sweep(state)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"! runde feilet: {e}", file=sys.stderr)
        if a.once:
            break
        if a.minutes and (time.time() - t0) / 60 >= a.minutes:
            break
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
