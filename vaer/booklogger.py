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
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ── Parametre — skal matche backtesten ────────────────────────────────────
THRESHOLD = 0.80        # kjøp første gang prisen krysser denne
MAX_ENTRY = 0.99        # gapvakt: appen blokkerer fyll >= 99,9¢
MIN_VOLUME = 25.0       # LAVT med vilje: backtestens $250 er SLUTTvolum.
                        # Ved krysningen har markedet ofte omsatt for mindre.
                        # Vi logger volumet og filtrerer ved analyse i stedet.
STAKE = 100.0           # flat innsats per handel
POLL_SEC = 120          # værbøker beveger seg sakte; 2 min er rikelig
REQ_PAUSE = 0.15        # pause mellom CLOB-kall

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

LOG = "vaer-book.jsonl"
STATE = "vaer-state.json"


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


# ── Kategorisering — ORDRETT KOPI fra ingest.py ────────────────────────────
# Ikke omskriv denne. Endres TAG_MAP i ingest.py, kopier den hit igjen,
# ellers ser tørrkjøringen et annet utvalg enn backtesten gjorde.
PRIORITY = ["crypto", "esports", "weather", "stocks",
            "sports", "politics", "economy", "culture"]

TAG_MAP = {
    "crypto": {
        "crypto", "crypto-prices", "bitcoin", "ethereum", "solana", "memecoins",
        "stablecoins", "defi", "nft", "altcoins", "xrp", "dogecoin", "bnb",
        "cardano", "chainlink", "avalanche", "litecoin", "pepe", "shiba-inu",
        "crypto-etf", "hourly-crypto",
    },
    "esports": {
        "esports", "counter-strike-2", "counter-strike", "cs2", "league-of-legends",
        "dota-2", "valorant", "call-of-duty", "rocket-league", "overwatch",
        "starcraft", "apex-legends",
    },
    "weather": {"weather", "climate", "temperature", "hurricane", "hurricanes"},
    "stocks": {
        "stocks", "earnings", "equities", "ipo", "etf", "nasdaq", "sp500",
        "companies", "tech-stocks",
    },
    "sports": {
        "sports", "nba", "nfl", "mlb", "nhl", "soccer", "epl", "ufc", "mma",
        "tennis", "golf", "olympics", "formula-1", "cricket", "basketball",
        "baseball", "football", "hockey", "boxing", "atp", "wta", "itf",
        "champions-league", "europa-league", "fifa-world-cup", "wnba", "ncaa",
        "college-football", "college-basketball", "rugby", "cycling", "chess",
        "major-league-cricket", "nba-playoffs", "nba-finals", "wc-tournament-futures",
    },
    "politics": {
        "politics", "elections", "geopolitics", "us-current-affairs", "world",
        "trump", "foreign-affairs", "international-affairs", "house-races",
        "us-elections", "democratic-party", "republican-party", "legal-cases",
        "senate-races", "governor-races",
    },
    "economy": {
        "economy", "fed", "macro", "finance", "business", "economics",
        "macro-graph", "macro-single", "inflation", "fdic", "tariffs",
    },
    "culture": {
        "pop-culture", "movies", "music", "awards", "oscars", "entertainment",
        "tv", "celebrities", "openai", "ai", "science", "space",
    },
}

NOISE_TAGS = {"all", "recurring", "hide-from-new", "multi-strikes", "games",
              "1h", "1d", "weekly", "monthly", "daily", "featured", "new"}

CATEGORY_KEYWORDS = {
    "crypto": ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto",
               "doge", "dogecoin", "xrp", "memecoin", "altcoin", "bnb", "cardano",
               "ada", "chainlink", "avax", "litecoin", "pepe"],
    "weather": ["highest temperature", "lowest temperature", "rainfall",
                "snowfall", "hurricane"],
    "stocks": ["up or down on", "beat quarterly earnings", "market cap",
               "finish week of", "ipo day"],
    "politics": ["election", "president", "senate", "congress", "trump", "biden",
                 "parliament", "minister", "vote", "poll", "ceasefire", "impeach"],
    "sports": ["nba", "nfl", "mlb", "nhl", "ufc", "premier league", "champions league",
               "world cup", "super bowl", "grand slam", "wimbledon", "goalscorer",
               "playoffs", "draft", "itf", "o/u", "spread", "handicap", "exact score",
               "set 1 winner", "set 2 winner", "set 3 winner"],
    "economy": ["fed", "rate hike", "rate cut", "inflation", "cpi", "gdp",
                "recession", "jobs report", "tariff"],
}

_WORD_RES = {cat: [re.compile(r"\b" + re.escape(w) + r"\b") for w in words]
             for cat, words in CATEGORY_KEYWORDS.items()}
_TAG_TO_CAT = {slug: cat for cat, slugs in TAG_MAP.items() for slug in slugs}


def tag_labels(market):
    out = []
    for t in (market.get("tags") or []):
        label = t.get("slug") or t.get("label") if isinstance(t, dict) else str(t)
        if label:
            out.append(str(label).lower())
    # Gamma legger av og til taggene paa event-objektet i stedet — DETTE
    # manglet i forste versjon og er sannsynligvis hovedfeilen.
    if not out:
        for ev in (market.get("events") or []):
            for t in (ev.get("tags") or []):
                label = t.get("slug") or t.get("label") if isinstance(t, dict) else str(t)
                if label:
                    out.append(str(label).lower())
    return out


def categorize(market):
    hits = {_TAG_TO_CAT[l] for l in tag_labels(market)
            if l not in NOISE_TAGS and l in _TAG_TO_CAT}
    if hits:
        for cat in PRIORITY:
            if cat in hits:
                return cat
    text = str(market.get("question", "")).lower()
    for cat in PRIORITY:
        for r in _WORD_RES.get(cat, []):
            if r.search(text):
                return cat
    return "other"


OFFSET_LIMIT = 2000
HORIZON_DAYS = 7        # vaermarkeder gjores opp raskt
DISCOVER_SEC = 1800     # hvor ofte markedslista bygges paa nytt
WEATHER_TAG = "84"      # Gammas tag-id for weather — funnet med `probe`
PAGE = 100              # Gamma gir 100 om gangen selv naar man ber om 500

_cache = {"tid": 0.0, "markeder": []}


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def active_weather_markets(force=False):
    """Aapne vaermarkeder innen HORIZON_DAYS, hentet direkte paa tag.

    Skanningen over hele universet er borte: sport fylte offsetgrensen selv
    i timesvinduer, og funnet ble aldri ferdig. Med tag_id spor vi Gamma om
    de riktige med én gang.

    Taggen er bare et forfilter — categorize() fra ingest.py avgjor fortsatt,
    slik at torrkjoringen ser NOYAKTIG samme utvalg som backtesten gjorde.
    """
    if not force and _cache["markeder"] and time.time() - _cache["tid"] < DISCOVER_SEC:
        return _cache["markeder"]

    naa = datetime.now(timezone.utc)
    lo, hi = _iso(naa), _iso(naa + timedelta(days=HORIZON_DAYS))

    raa, offset = [], 0
    while offset < OFFSET_LIMIT:
        batch = get(f"{GAMMA}/markets", {
            "tag_id": WEATHER_TAG, "related_tags": "true",
            "limit": PAGE, "offset": offset,
            "end_date_min": lo, "end_date_max": hi,
            "include_tag": "true",
        })
        if not batch:
            break
        raa.extend(batch)
        offset += len(batch)
        if len(batch) < PAGE:
            break
        time.sleep(0.35)

    sett, aapne, forkastet, ut = set(), 0, 0, []
    for m in raa:
        mid = str(m.get("id") or m.get("conditionId") or "")
        if mid in sett:
            continue
        sett.add(mid)
        if m.get("closed") or m.get("umaResolutionStatus") == "resolved":
            continue
        aapne += 1
        if categorize(m) != "weather":
            forkastet += 1        # taggen sa vaer, backtestens regel sa noe annet
            continue
        if float(m.get("volumeNum") or m.get("volume") or 0) >= MIN_VOLUME:
            ut.append(m)

    print(f"  {offset // PAGE + 1} sider | unike {len(sett)} | aapne {aapne} | "
          f"forkastet av categorize {forkastet} | over ${MIN_VOLUME:.0f} {len(ut)}",
          file=sys.stderr)
    if not raa:
        print("  !! tag-kallet ga null — er tag-id 84 fortsatt riktig? "
              "kjor `probe` paa nytt", file=sys.stderr)

    _cache["markeder"], _cache["tid"] = ut, time.time()
    return ut


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
            s = json.load(f)
        s.setdefault("under", {})   # eldre tilstandsfiler
        return s
    return {"seen": {}, "under": {}}


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
    hits, kalde = 0, [0]

    for m in markets:
        tids, outs = token_ids(m), outcomes(m)
        for i, tid in enumerate(tids):
            if tid in state["seen"]:
                continue          # regelen kjøper FØRSTE krysning, aldri igjen

            last, mid = signal_prices(tid)
            time.sleep(REQ_PAUSE)
            sig = last if last is not None else mid
            if sig is None:
                continue
            if sig < THRESHOLD:
                # Sett under terskel — herfra teller en oppgang som ekte krysning.
                state["under"][tid] = True
                continue

            # Kaldstart: markedet laa allerede over 80¢ da vi begynte aa se paa
            # det. Vi vet ikke naar det krysset, saa prisen her er ikke prisen
            # regelen ville betalt. Logges for innsyn, holdes utenfor tallene.
            kryssing = state["under"].pop(tid, False)

            book = get(f"{CLOB}/book", {"token_id": tid})
            time.sleep(REQ_PAUSE)
            asks = (book or {}).get("asks") or []
            vwap, shares, levels, filled, avail = walk_asks(asks)
            best_ask = min((float(a["price"]) for a in asks), default=None)

            status = "filled" if filled else ("partial" if shares else "empty")
            if not kryssing:
                status = "kaldstart"
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
            if kryssing:
                print(f"  + {status:7} {rec['slip_c']}¢  {(rec['question'] or '')[:52]}",
                      file=sys.stderr)
            else:
                kalde[0] += 1

    if kalde[0]:
        print(f"  ({kalde[0]} laa allerede over terskel — kaldstart, teller ikke)",
              file=sys.stderr)
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

    kalde = [r for r in rows if r["status"] == "kaldstart"]
    rows = [r for r in rows if r["status"] != "kaldstart"]
    if not rows:
        print(f"Ingen ekte krysninger ennaa ({len(kalde)} kaldstart, holdt utenfor).")
        return
    filled = [r for r in rows if r["status"] == "filled"]
    slips = sorted(r["slip_c"] for r in filled if r["slip_c"] is not None)
    n_bad = sum(1 for r in rows if r["status"] != "filled")

    clusters = defaultdict(list)
    for r in rows:
        clusters[cluster_key(r)].append(r)
    days = {r["ts"][:10] for r in rows}

    print(f"\nEkte krysninger   {len(rows)}")
    print(f"  kaldstart       {len(kalde)}  (laa over terskel ved oppstart — utenfor)")
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



# ── Sondering ─────────────────────────────────────────────────────────────
def probe():
    """Proever aa finne en maate aa be Gamma om BARE vaermarkeder.

    Skanningen over hele universet er for treg: sport fyller offsetgrensen
    selv i timesvinduer. Lykkes én av disse, forsvinner problemet helt.
    Kjor denne én gang, se hvilken som gir treff, si fra hvilken.
    """
    naa = datetime.now(timezone.utc)
    lo, hi = _iso(naa), _iso(naa + timedelta(days=HORIZON_DAYS))

    def vis(navn, data, plukk=None):
        if data is None:
            print(f"  {navn:52} FEILET"); return
        if isinstance(data, dict):
            data = data.get("data") or data.get("events") or [data]
        n = len(data)
        v = sum(1 for m in data if categorize(m) == "weather") if plukk != "raa" else "?"
        smak = ""
        for m in data:
            if categorize(m) == "weather":
                smak = f"  <- {str(m.get('question') or m.get('title'))[:44]}"
                break
        print(f"  {navn:52} {n:5} treff | vaer {v}{smak}")
        return data

    print("\n=== 1. finn tag-id for weather ===", file=sys.stderr)
    tag_id = None
    for u in (f"{GAMMA}/tags/slug/weather", f"{GAMMA}/tags?slug=weather"):
        d = get(u)
        if d:
            t = d[0] if isinstance(d, list) and d else d
            tag_id = (t or {}).get("id")
            print(f"  {u.split('/')[-1]:52} -> id={tag_id}")
            if tag_id:
                break

    print("\n=== 2. markeder filtrert paa tag ===")
    if tag_id:
        vis(f"markets?tag_id={tag_id}",
            get(f"{GAMMA}/markets", {"tag_id": tag_id, "limit": 500,
                                     "end_date_min": lo, "end_date_max": hi,
                                     "include_tag": "true"}))
        vis(f"markets?tag_id={tag_id}&related_tags=true",
            get(f"{GAMMA}/markets", {"tag_id": tag_id, "related_tags": "true",
                                     "limit": 500, "end_date_min": lo,
                                     "end_date_max": hi, "include_tag": "true"}))
    vis("markets?tag_slug=weather",
        get(f"{GAMMA}/markets", {"tag_slug": "weather", "limit": 500,
                                 "end_date_min": lo, "end_date_max": hi,
                                 "include_tag": "true"}))

    print("\n=== 3. events (langt faerre enn markeder) ===")
    vis("events?tag_slug=weather&closed=false",
        get(f"{GAMMA}/events", {"tag_slug": "weather", "closed": "false",
                                "limit": 500, "include_tag": "true"}))
    if tag_id:
        vis(f"events?tag_id={tag_id}&closed=false",
            get(f"{GAMMA}/events", {"tag_id": tag_id, "closed": "false",
                                    "limit": 500, "include_tag": "true"}))
    vis("events?closed=false  (alle, filtrert her)",
        get(f"{GAMMA}/events", {"closed": "false", "limit": 500,
                                "end_date_min": lo, "end_date_max": hi,
                                "include_tag": "true"}))

    print("\n=== 4. fritekstsok ===")
    vis("markets?slug_contains=temperature",
        get(f"{GAMMA}/markets", {"slug_contains": "temperature", "limit": 500,
                                 "include_tag": "true"}))
    print("\nSi fra hvilken linje som gir treff paa vaer.\n")


# ── main ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["watch", "report", "probe"])
    ap.add_argument("--once", action="store_true", help="én runde, så avslutt")
    ap.add_argument("--minutes", type=int, default=0, help="stopp etter N minutter")
    a = ap.parse_args()

    if a.cmd == "report":
        return report()
    if a.cmd == "probe":
        return probe()

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
