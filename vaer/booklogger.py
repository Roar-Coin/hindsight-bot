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

# Egne filer per kjoring. To jobber kan da aldri skrive til samme fil, og
# git faar ingenting aa slaa sammen. Rebase paa en fil som bare vokser paa
# slutten gir konflikt hver gang, og konfliktmarkorene odela state-fila.
RUN = os.environ.get("GITHUB_RUN_ID") or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
LOG_DIR, STATE_DIR = "logg", "state"
LOG = os.path.join(LOG_DIR, f"{RUN}.jsonl")
STATE = os.path.join(STATE_DIR, f"{RUN}.json")


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
DISCOVER_SEC = 300      # funnet er billig naa — 11 kall — saa kjor det ofte
WATCH_FROM = 0.70       # CLOB-poll bare tokens Gamma sier ligger her eller over
WEATHER_TAG = "84"      # Gammas tag-id for weather — funnet med `probe`
PAGE = 100              # Gamma gir 100 om gangen selv naar man ber om 500

_cache = {"tid": 0.0, "markeder": []}


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gamma_priser(m):
    """Gamma legger naavaerende pris rett i markedsobjektet. Gratis — den
    folger med funnkallet, saa vi slipper aa sporre CLOB om 2100 tokens."""
    raa = m.get("outcomePrices")
    if isinstance(raa, str):
        try:
            raa = json.loads(raa)
        except Exception:
            return []
    try:
        return [float(x) for x in (raa or [])]
    except Exception:
        return []


def vaktliste(state, force=False):
    """To trinn:
      1. Hent alle vaermarkeder paa tag (11 kall) og les Gamma-prisen deres.
         Alt under terskel foeres inn i under-registeret — det er dette som
         gjor at en senere oppgang teller som ekte krysning.
      2. Returner BARE tokens fra WATCH_FROM og opp. Bare de blir CLOB-pollet.

    Var: 2100 tokens x 2 CLOB-kall = 11 minutter per runde.
    Naa: 11 Gamma-kall + CLOB bare paa de faa som er i naerheten.
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

    sett, aapne, forkastet, tokens, uten_pris = set(), 0, 0, 0, 0
    ut = []
    for m in raa:
        mid = str(m.get("id") or m.get("conditionId") or "")
        if mid in sett:
            continue
        sett.add(mid)
        if m.get("closed") or m.get("umaResolutionStatus") == "resolved":
            continue
        aapne += 1
        if categorize(m) != "weather":
            forkastet += 1
            continue
        if float(m.get("volumeNum") or m.get("volume") or 0) < MIN_VOLUME:
            continue

        tids, priser = token_ids(m), _gamma_priser(m)
        for i, tid in enumerate(tids):
            tokens += 1
            if tid in state["seen"]:
                continue
            gp = priser[i] if i < len(priser) else None
            if gp is None:
                uten_pris += 1
                ut.append({"m": m, "i": i, "tid": tid})   # ta med, sjekk via CLOB
                continue
            if gp < THRESHOLD:
                # Lagre NAAR vi saa den under terskel. Ligger jobben nede en
                # time, kan et marked ha krysset og loept videre til 91¢ for
                # vi ser igjen. Da er det ikke prisen regelen ville betalt.
                state["under"][tid] = time.time()
            if gp >= WATCH_FROM:
                ut.append({"m": m, "i": i, "tid": tid})

    print(f"  {offset // PAGE + 1} sider | vaermarkeder {aapne - forkastet} | "
          f"tokens {tokens} | paa vaktliste {len(ut)}"
          + (f" | uten Gamma-pris {uten_pris}" if uten_pris else ""),
          file=sys.stderr)
    if not raa:
        print("  !! tag-kallet ga null — kjor `probe` paa nytt", file=sys.stderr)

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
    """Tre referanser. Backtesten leste handelsprisserien, saa `last` ligner
    mest — men logg alle, saa slipper vi aa gjette i etterkant."""
    last = get(f"{CLOB}/last-trade-price", {"token_id": tid})
    mid = get(f"{CLOB}/midpoint", {"token_id": tid})
    f = lambda d, k="price": float(d[k]) if d and k in d else None
    return f(last), (f(mid, "mid") if mid and "mid" in mid else f(mid))


def walk_asks(asks, stake=STAKE):
    """Gaar gjennom ask-siden til $100 er brukt opp. Returnerer VWAP, antall
    aksjer, antall nivaaer vi spiste, om vi ble fylt helt, og hvor mye
    boken faktisk kunne ta."""
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
    """Slaar sammen tilstanden fra alle tidligere kjoringer. `seen` og `under`
    er rene oppslag, saa union er riktig sammenslaaing.

    Taaler odelagte filer: en enkelt korrupt state-fil drepte fire av fem
    bolker forrige gang. Naa flyttes den til side og resten leses videre."""
    s = {"seen": {}, "under": {}}
    os.makedirs(STATE_DIR, exist_ok=True)
    for navn in sorted(os.listdir(STATE_DIR)):
        if not navn.endswith(".json"):
            continue
        sti = os.path.join(STATE_DIR, navn)
        try:
            with open(sti) as f:
                d = json.load(f)
        except Exception as e:
            os.replace(sti, sti + ".odelagt")
            print(f"  ! {navn} kunne ikke leses ({e}) — lagt til side", file=sys.stderr)
            continue
        s["seen"].update(d.get("seen") or {})
        s["under"].update(d.get("under") or {})
    return s


def save_state(s):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, STATE)


def append(rec):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── Én runde ──────────────────────────────────────────────────────────────
def sweep(state):
    watch = vaktliste(state)
    hits, kalde = 0, [0]

    for w in watch:
        tid, m, i = w["tid"], w["m"], w["i"]
        if tid in state["seen"]:
            continue

        last, mid_p = signal_prices(tid)
        time.sleep(REQ_PAUSE)
        sig = last if last is not None else mid_p
        if sig is None:
            continue
        if sig < THRESHOLD:
            state["under"][tid] = time.time()
            continue

        # Kaldstart: laa allerede over terskel da vi begynte aa se. Vi vet ikke
        # naar den krysset, saa prisen her er ikke prisen regelen ville betalt.
        sist_under = state["under"].pop(tid, None)
        kryssing = sist_under is not None
        # Hvor gammel kan krysningen vaere? Eldre observasjon = losere maaling.
        # NB: bool er en int i Python, saa `True` fra eldre tilstandsfiler ville
        # gitt alder paa 30 millioner minutter. Maa utelukkes eksplisitt.
        alder = (round((time.time() - sist_under) / 60, 1)
                 if isinstance(sist_under, (int, float))
                 and not isinstance(sist_under, bool) else None)

        book = get(f"{CLOB}/book", {"token_id": tid})
        time.sleep(REQ_PAUSE)
        asks = (book or {}).get("asks") or []
        vwap, shares, levels, filled, avail = walk_asks(asks)
        best_ask = min((float(a["price"]) for a in asks), default=None)

        status = "filled" if filled else ("partial" if shares else "empty")
        if not kryssing:
            status = "kaldstart"
        outs = outcomes(m)
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
            "sig_mid": mid_p,
            "best_ask": best_ask,
            "fill_vwap": vwap,
            "shares": round(shares, 2),
            "levels": levels,
            "filled": filled,
            "notional_available": round(avail, 2),
            "status": status,
            "alder_min": alder,      # minutter siden vi sist saa den under 80¢
            "slip_c": round((vwap - sig) * 100, 3) if vwap else None,
            "slip_vs_mid_c": round((vwap - mid_p) * 100, 3) if vwap and mid_p else None,
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
    filer = []
    if os.path.isdir(LOG_DIR):
        filer = [os.path.join(LOG_DIR, n) for n in sorted(os.listdir(LOG_DIR))
                 if n.endswith(".jsonl")]
    if os.path.exists("vaer-book.jsonl"):
        filer.append("vaer-book.jsonl")        # fra for filene ble delt opp
    if not filer:
        print("Ingen logg ennå.")
        return
    rows = []
    for sti in filer:
        for l in open(sti):
            l = l.strip()
            if not l or l[0] in "<>=":         # eventuelle konfliktmarkorer
                continue
            try:
                rows.append(json.loads(l))
            except Exception:
                pass
    print(f"({len(filer)} loggfiler)")
    if not rows:
        print("Tom logg.")
        return

    kalde = [r for r in rows if r["status"] == "kaldstart"]
    rows = [r for r in rows if r["status"] != "kaldstart"]
    if not rows:
        print(f"Ingen ekte krysninger ennaa ({len(kalde)} kaldstart, holdt utenfor).")
        return
    ferske = [r for r in rows if r.get("alder_min") is not None
              and r["alder_min"] <= 5]
    filled = [r for r in rows if r["status"] == "filled"]
    slips = sorted(r["slip_c"] for r in filled if r["slip_c"] is not None)
    n_bad = sum(1 for r in rows if r["status"] != "filled")

    clusters = defaultdict(list)
    for r in rows:
        clusters[cluster_key(r)].append(r)
    days = {r["ts"][:10] for r in rows}

    print(f"\nEkte krysninger   {len(rows)}")
    print(f"  herav ferske    {len(ferske)}  (sett under 80¢ for under 5 min siden)")
    print(f"  kaldstart       {len(kalde)}  (laa over terskel ved oppstart — utenfor)")
    print(f"  fylt            {len(filled)}")
    print(f"  ufyllbare       {n_bad}  ({n_bad / len(rows):.0%})   [stopp ved 33 %]")
    # To maal, og de svarer paa hver sin ting:
    #   mot midtpunkt = ren gjennomforingskostnad (spread + dybde), samtidig maalt
    #   mot siste handel = det samme PLUSS hvor foreldet backtestens
    #     inngangspris var. Store negative tall her betyr stale referansepris,
    #     ikke en fordel.
    mids = sorted(r["slip_vs_mid_c"] for r in filled
                  if r.get("slip_vs_mid_c") is not None)
    if mids:
        m_avg = sum(mids) / len(mids)
        print(f"\nGjennomføringskostnad mot MIDTPUNKT  (det ekte kostnadstallet)")
        print(f"  snitt           {m_avg:.2f}¢")
        print(f"  median          {mids[len(mids) // 2]:.2f}¢")
        print(f"  p95             {mids[min(len(mids)-1, int(0.95*len(mids)))]:.2f}¢")
        print(f"  verste          {mids[-1]:.2f}¢")
        print(f"  negative        {sum(1 for x in mids if x < 0)} av {len(mids)}"
              f"   (bor vaere naer null — under midtpunkt er uvanlig)")

        # Snittet avgjor lonnsomheten — samlet resultat er N x (fordel - snitt).
        # Men med tung hale konvergerer det sakte, saa punktestimatet alene
        # sier ingenting om vi har nok data. Standardfeilen sier det.
        n = len(mids)
        var = sum((x - m_avg) ** 2 for x in mids) / (n - 1) if n > 1 else 0.0
        se = (var / n) ** 0.5 if n > 1 else float("inf")
        lo_ci, hi_ci = m_avg - 2 * se, m_avg + 2 * se
        print(f"\n  snitt med 95 %-intervall   {m_avg:.2f}¢  [{lo_ci:.2f} – {hi_ci:.2f}]")
        print(f"  andel over 0,8¢            {sum(1 for x in mids if x > 0.8)/n:.0%}"
              f"   (halens tyngde — ikke et kriterium i seg selv)")

        for navn, tak in (("+1,3 pp", 1.41), ("+0,7 pp", 1.00)):
            if hi_ci < tak:
                dom = "PASSERER"
            elif lo_ci > tak:
                dom = "BRUDD"
            else:
                dom = "for tidlig — intervallet spenner over taket"
            print(f"  mot tak {tak:.2f}¢ (fordel {navn}):  {dom}")

        # Naar er vi ferdige? Naar intervallet ikke lenger krysser taket.
        if se != float("inf") and se > 0:
            for tak in (1.41, 1.00):
                if lo_ci <= tak <= hi_ci:
                    trengs = int(n * (2 * se / max(abs(m_avg - tak), 1e-9)) ** 2)
                    print(f"  -> ca. {trengs} fyll trengs for aa avgjore mot {tak:.2f}¢"
                          f" (har {n})")
                    break

    if slips:
        avg = sum(slips) / len(slips)
        stale = [x for x in slips if x < -2.0]
        p95 = slips[min(len(slips) - 1, int(0.95 * len(slips)))]
        print(f"\nMot SISTE HANDEL  (= kostnad + foreldet referansepris)")
        print(f"  snitt           {avg:.2f}¢")
        print(f"  median          {slips[len(slips) // 2]:.2f}¢")
        print(f"  p95             {p95:.2f}¢")
        print(f"  verste          {slips[-1]:.2f}¢")
        print(f"  backtest antok  0,50¢")
        print(f"  under -2¢       {len(stale)} av {len(slips)}"
              f"   ({len(stale)/len(slips):.0%} der siste handel laa langt over boken)")
        if len(stale) > 0.1 * len(slips):
            print("  -> backtestens inngangspris er ofte foreldet i tynne marked.")
            print("     Det er et gyldighetsproblem ved backtesten, ikke ved fyllene.")
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
