#!/usr/bin/env python3
"""
makertest.py — lonner det seg aa LEGGE spreaden i stedet for aa krysse den?

Torrkjoringen viste at vi betaler ~5,6¢ i halv spread paa vaermarkeder. Dette
skriptet maaler den andre siden: hva tjener den som LIGGER der og tar imot?

Ingen kapital, ingen ordre. Vi simulerer en hvilende ordre paa beste bud og
foelger hva prisen gjor etterpaa.

DET AVGJORENDE TALLET er ikke spreaden vi fanger — det er markout: hvor mye
prisen har beveget seg MOT oss 5, 30 og 120 minutter etter fyll, pluss hva
markedet til slutt gjorde opp til. En hvilende ordre blir nemlig fylt nettopp
naar noen har grunn til aa handle mot den.

    fortjeneste = fanget spread - ugunstig utvalg - oppgjorstap

Ligger markout naer null, finnes det noe. Er den storre enn spreaden, er du
langsom mat for raskere aktorer.

Bruk:
    python makertest.py watch            # loop, logger til maker/<kjoring>.jsonl
    python makertest.py watch --minutes 25
    python makertest.py report
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from booklogger import (CLOB, GAMMA, HORIZON_DAYS, OFFSET_LIMIT, PAGE,
                        WEATHER_TAG, _gamma_priser, _iso, categorize, get,
                        outcomes, token_ids)

# ── Parametre ─────────────────────────────────────────────────────────────
TICK = 0.01           # minste prissteg
BAND = (0.10, 0.90)   # kvoter kun her; ytterkantene er en annen ovelse
MIN_VOLUME = 250.0
MIN_SPREAD = 2 * TICK  # under dette er det ikke noe aa fange
SIZE = 100.0          # $ per simulert ordre
POLL_SEC = 120
MARKOUTS = (5, 30, 120)   # minutter etter fyll
MAX_KVOTER = 400      # tak paa samtidige simulerte ordrer
MIN_TIMER_TIL_OPPGJOR = 6   # ikke kvoter markeder som snart gjores opp. Naer
                            # oppgjor dumper informerte den tapende siden, og
                            # et hvilende bud tar imot. Det er retningsvedd,
                            # ikke market making.
MAX_LIV_MIN = 30      # kanseller ufylte ordrer etter dette. En ekte maker
                      # trekker kvoten naar markedet beveger seg; en ordre som
                      # ligger i 12 timer fanger hver eneste nedtur og blir
                      # fylt 100 % av tiden — men bare ugunstig.
REQ_PAUSE = 0.15

RUN = os.environ.get("GITHUB_RUN_ID") or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")

# Kategori settes med --tag. Hver kategori faar egne mapper, ellers blandes
# vaerdata og cryptodata i samme rapport.
TAG = {"slug": "weather", "id": None}
LOG_DIR = STATE_DIR = LOG = STATE = None


def sett_kategori(slug):
    global LOG_DIR, STATE_DIR, LOG, STATE
    TAG["slug"] = slug
    LOG_DIR = "maker" if slug == "weather" else f"maker-{slug}"
    STATE_DIR = LOG_DIR + "-state"
    LOG = os.path.join(LOG_DIR, f"{RUN}.jsonl")
    STATE = os.path.join(STATE_DIR, f"{RUN}.json")


sett_kategori("weather")


# ── Bok ───────────────────────────────────────────────────────────────────
def topp(tid):
    """Beste bud og beste ask, med storrelsen som ligger foran oss paa budet."""
    b = get(f"{CLOB}/book", {"token_id": tid})
    if not b:
        return None, None, 0.0
    bids = [(float(x["price"]), float(x["size"])) for x in (b.get("bids") or [])]
    asks = [float(x["price"]) for x in (b.get("asks") or [])]
    if not bids or not asks:
        return None, None, 0.0
    bb = max(bids)[0]
    foran = sum(sz for p, sz in bids if abs(p - bb) < 1e-9) * bb
    return bb, min(asks), foran


DATA_API = "https://data-api.polymarket.com"


def handler(cond, tid, etter_ts, limit=200):
    """Ekte handelsprints for ETT token, nyere enn etter_ts.

    To ting maatte paa plass, begge funnet ved sondering:
      - asset_id blir IGNORERT av data-api. Den ga en global strom av handler
        fra hele Polymarket, og det var grunnen til at tre versjoner paa rad
        ga 100 % fyllrate. market=<conditionId> filtrerer derimot riktig.
      - Begge sider av en handel rapporteres, hver i sitt tokens prisuttrykk
        (Ja kjopt til 0,56 = Nei solgt til 0,44). Vi filtrerer paa `asset`
        slik at prisene er i VAART tokens termer.
    """
    d = get(f"{DATA_API}/trades", {"market": cond, "limit": limit})
    if not isinstance(d, list):
        return []
    mitt = str(tid)
    ut = []
    for t in d:
        if str(t.get("asset") or "") != mitt:
            continue
        try:
            ts = float(t.get("timestamp") or 0)
            if ts > 1e11:
                ts /= 1000.0
            if ts <= etter_ts:
                continue
            ut.append((ts, float(t["price"]), float(t.get("size") or 0),
                       str(t.get("side") or "").upper()))
        except Exception:
            continue
    return sorted(ut)


def siste(tid):
    d = get(f"{CLOB}/last-trade-price", {"token_id": tid})
    return float(d["price"]) if d and "price" in d else None


def midt(tid):
    d = get(f"{CLOB}/midpoint", {"token_id": tid})
    if not d:
        return None
    for k in ("mid", "price"):
        if k in d:
            return float(d[k])
    return None


# ── Markeder ──────────────────────────────────────────────────────────────
def referanse(tid, cond=None, i=0):
    """Pris til markout-maaling, med fallback.

    /midpoint gir 404 naar markedet har gjort opp. Uten fallback ble markouten
    None og falt ut av statistikken — altsaa forsvant nettopp de posisjonene
    som gikk til null. Det er en skjevhet i optimistisk retning."""
    p = midt(tid)
    if p is not None:
        return p, "mid"
    p = siste(tid)
    if p is not None:
        return p, "siste"
    # NB: her maa markedet hentes PAA NYTT. Et lagret oyeblikksbilde fra
    # kvotetidspunktet gir prisen vi allerede kjente, ikke oppgjorsverdien,
    # og ville rapportert markout ~0 for nettopp de markedene som gjorde opp.
    if cond:
        d = get(f"{GAMMA}/markets", {"condition_ids": cond, "limit": 1})
        if isinstance(d, list) and d:
            pr = _gamma_priser(d[0])
            if i < len(pr):
                return pr[i], "gamma-fersk"
    return None, None


def markeder(tag=None):
    if tag is None:
        if TAG["id"] is None:
            if TAG["slug"] == "weather":
                TAG["id"] = WEATHER_TAG
            else:
                d = get(f"{GAMMA}/tags/slug/{TAG['slug']}")
                if isinstance(d, list):
                    d = d[0] if d else None
                TAG["id"] = (d or {}).get("id")
                if not TAG["id"]:
                    print(f"fant ingen tag-id for '{TAG['slug']}'", file=sys.stderr)
                    return []
        tag = TAG["id"]
    naa = datetime.now(timezone.utc)
    lo, hi = _iso(naa), _iso(naa + timedelta(days=HORIZON_DAYS))
    ut, offset = [], 0
    while offset < OFFSET_LIMIT:
        batch = get(f"{GAMMA}/markets", {
            "tag_id": tag, "related_tags": "true", "limit": PAGE,
            "offset": offset, "end_date_min": lo, "end_date_max": hi,
            "include_tag": "true"})
        if not batch:
            break
        for m in batch:
            if m.get("closed"):
                continue
            if float(m.get("volumeNum") or m.get("volume") or 0) < MIN_VOLUME:
                continue
            if categorize(m) != TAG["slug"]:
                continue
            ut.append(m)
        offset += len(batch)
        if len(batch) < PAGE:
            break
        time.sleep(0.35)
    return ut


# ── Tilstand ──────────────────────────────────────────────────────────────
def load_state():
    s = {"kvoter": {}}
    os.makedirs(STATE_DIR, exist_ok=True)
    for n in sorted(os.listdir(STATE_DIR)):
        if not n.endswith(".json"):
            continue
        p = os.path.join(STATE_DIR, n)
        try:
            with open(p) as f:
                s["kvoter"].update((json.load(f).get("kvoter") or {}))
        except Exception as e:
            os.replace(p, p + ".odelagt")
            print(f"  ! {n} ulesbar ({e}) — lagt til side", file=sys.stderr)
    return s


def save_state(s):
    os.makedirs(STATE_DIR, exist_ok=True)
    t = STATE + ".tmp"
    with open(t, "w") as f:
        json.dump(s, f)
    os.replace(t, STATE)


def skriv(rec):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── Én runde ──────────────────────────────────────────────────────────────
def sweep(state):
    kv = state["kvoter"]
    naa = time.time()

    # 1. Foelg opp ordrer som allerede ligger ute
    ferdige = []
    for tid, q in list(kv.items()):
        if not q.get("fylt"):
            # Fyll avgjores av EKTE handler etter at vi la ordren.
            #
            # Vi laa bakerst i koen paa vaart nivaa, med `foran_i_ko` dollar
            # foran oss. En SELL-handel til vaar pris eller lavere spiser koen
            # nedenfra. Naar det akkumulerte salgsvolumet passerer det som laa
            # foran oss, er turen kommet til oss.
            #
            # Salg UNDER vaar pris feier hele nivaaet og tar oss uansett.
            nye = handler(q.get("cond"), tid,
                          q.get("sett_til") or q["lagt"])
            time.sleep(REQ_PAUSE)
            if nye:
                q["sett_til"] = nye[-1][0]
            spist = q.get("spist", 0.0)
            for ts, pris, storr, side in nye:
                if side != "BUY" and pris < q["bud"] - 1e-9:
                    # Noen solgte UNDER vaart bud: boken handlet gjennom vaart
                    # nivaa, og prisprioritet gjor at vi ble tatt underveis.
                    q["fylt"], q["type"] = ts, "feid"
                    break
                if abs(pris - q["bud"]) < 1e-9 and side != "BUY":
                    spist += storr * pris
                    if spist >= q["foran_i_ko"]:
                        q["fylt"], q["type"] = ts, "ko_naadd"
                        break
            q["spist"] = round(spist, 2)

            if not q.get("fylt"):
                if naa - q["lagt"] > MAX_LIV_MIN * 60:
                    skriv({**q, "tid": tid, "utfall": "aldri_fylt"})
                    ferdige.append(tid)
                continue
            q["ventet_min"] = round((q["fylt"] - q["lagt"]) / 60, 1)
            q["mid_ved_fyll"] = referanse(tid, q.get("cond"), q.get("i", 0))[0]
            time.sleep(REQ_PAUSE)
            continue

        # fylt — mål markout ved hver horisont
        alder = (naa - q["fylt"]) / 60
        for h in MARKOUTS:
            n = f"mo{h}"
            if n not in q and alder >= h:
                m, kilde = referanse(tid, q.get("cond"), q.get("i", 0))
                time.sleep(REQ_PAUSE)
                # positiv = prisen gikk VAAR vei etter at vi kjopte
                q[n] = round((m - q["bud"]) * 100, 3) if m is not None else None
                q[f"kilde{h}"] = kilde

        if all(f"mo{h}" in q for h in MARKOUTS):
            skriv({**q, "tid": tid, "utfall": "maalt"})
            ferdige.append(tid)

    for t in ferdige:
        kv.pop(t, None)

    # 2. Legg nye ordrer
    nye = 0
    for m in markeder():
        if len(kv) >= MAX_KVOTER:
            break
        pr, tids, outs = _gamma_priser(m), token_ids(m), outcomes(m)
        for i, tid in enumerate(tids):
            if tid in kv or len(kv) >= MAX_KVOTER:
                continue
            p = pr[i] if i < len(pr) else None
            if p is None or not (BAND[0] <= p <= BAND[1]):
                continue
            slutt = m.get("endDate")
            if slutt:
                try:
                    igjen = (datetime.fromisoformat(slutt.replace("Z", "+00:00"))
                             - datetime.now(timezone.utc)).total_seconds() / 3600
                    if igjen < MIN_TIMER_TIL_OPPGJOR:
                        continue
                except Exception:
                    pass
            bb, ba, foran = topp(tid)
            time.sleep(REQ_PAUSE)
            if bb is None or ba - bb < MIN_SPREAD:
                continue
            kv[tid] = {
                "lagt": naa, "bud": bb, "ask": ba,
                "cond": m.get("conditionId"),
                "marked": {"outcomePrices": m.get("outcomePrices"),
                           "clobTokenIds": m.get("clobTokenIds")},
                "i": i,
                "spread_c": round((ba - bb) * 100, 2),
                "fanget_c": round((ba - bb) / 2 * 100, 2),  # vs midtpunkt
                "foran_i_ko": round(foran, 2),
                "volum": float(m.get("volumeNum") or m.get("volume") or 0),
                "sporsmaal": (m.get("question") or "")[:70],
                "utfall_navn": outs[i] if i < len(outs) else None,
                "slutt": m.get("endDate"),
            }
            nye += 1

    save_state(state)
    fylt = sum(1 for q in kv.values() if q.get("fylt"))
    print(f"  kvoter {len(kv)} (+{nye} nye) | fylt og ventende {fylt}",
          file=sys.stderr)
    return nye


# ── Rapport ───────────────────────────────────────────────────────────────
def report():
    rows = []
    if os.path.isdir(LOG_DIR):
        for n in sorted(os.listdir(LOG_DIR)):
            if n.endswith(".jsonl"):
                for l in open(os.path.join(LOG_DIR, n)):
                    l = l.strip()
                    if l and l[0] == "{":
                        try:
                            rows.append(json.loads(l))
                        except Exception:
                            pass
    if not rows:
        print("Ingen data ennaa.")
        return

    fylt = [r for r in rows if r["utfall"] == "maalt"]
    aldri = [r for r in rows if r["utfall"] == "aldri_fylt"]
    print(f"\nSimulerte ordrer  {len(rows)}")
    print(f"  fylt            {len(fylt)}  ({len(fylt)/len(rows):.0%})")
    print(f"  aldri fylt      {len(aldri)}   <- du tjener ingenting paa disse")
    v = sorted(r["ventet_min"] for r in fylt if r.get("ventet_min") is not None)
    if v:
        print(f"  ventetid til fyll: median {v[len(v)//2]:.0f} min, "
              f"raskeste {v[0]:.0f}, tregeste {v[-1]:.0f}"
              f"   [kvoten kanselleres etter {MAX_LIV_MIN}]")
    if fylt and len(fylt) / len(rows) > 0.8:
        print("  !! fyllrate over 80 % — sjekk at kvoten faktisk kanselleres.")
    feid = sum(1 for r in fylt if r.get("type") == "feid")
    print(f"  herav nivaa feid {feid} / ko naadd {len(fylt)-feid}")

    if not fylt:
        print("\nIngen fyll maalt ennaa.")
        return

    snitt = lambda xs: sum(xs) / len(xs) if xs else 0.0
    fanget = snitt([r["fanget_c"] for r in fylt])
    print(f"\nFanget spread     {fanget:.2f}¢ i snitt (mot midtpunkt)")

    # Oppgjor: gaar markedet til 0 eller 1, er markout ikke en prisbevegelse
    # men et utfall. Skilles ut, ellers drukner de vanlige tallene.
    oppgjort = [r for r in fylt if any(abs(r.get(f"mo{h}") or 0) > 20 for h in MARKOUTS)]
    if oppgjort:
        print(f"\n  {len(oppgjort)} av {len(fylt)} beveget seg over 20¢ — trolig")
        print(f"  oppgjor, ikke handel. Tas med i snittet under; de ER ekte tap,")
        print(f"  men de er en annen risiko enn ugunstig utvalg.")

    print(f"\nMARKOUT — hva prisen gjorde ETTER at vi ble fylt")
    print(f"  negativ = den gikk mot oss = ugunstig utvalg")
    netto = {}
    for h in MARKOUTS:
        xs = [r[f"mo{h}"] for r in fylt if r.get(f"mo{h}") is not None]
        if not xs:
            continue
        s = snitt(xs)
        netto[h] = fanget + s
        xs_s = sorted(xs)
        print(f"  etter {h:>3} min   {s:+.2f}¢   median {xs_s[len(xs)//2]:+.2f}¢"
              f"   verste {xs_s[0]:+.2f}¢")

    h_max = max(MARKOUTS)
    mangler = sum(1 for r in fylt if r.get(f"mo{h_max}") is None)
    ikke_mid = sum(1 for r in fylt if r.get(f"kilde{h_max}") not in (None, "mid"))
    if mangler or ikke_mid:
        print(f"\n  {mangler} av {len(fylt)} mangler markout helt;"
              f" {ikke_mid} maalt uten midtpunkt (marked gjort opp)")

    # De to risikoene er ulike i natur og maa skilles: prisbevegelse mot deg
    # (ugunstig utvalg) er noe en maker kan styre med raskere requoting.
    # Oppgjor er binaert og kan ikke styres bort.
    h = max(MARKOUTS)
    med = [r for r in fylt if r.get(f"mo{h}") is not None]
    oppg = [r for r in med if abs(r[f"mo{h}"]) > 20
            or r.get(f"kilde{h}") in ("siste", "gamma-fersk")]
    vanlig = [r for r in med if r not in oppg]
    if oppg and vanlig:
        so = snitt([r[f"mo{h}"] for r in oppg])
        sv = snitt([r[f"mo{h}"] for r in vanlig])
        print(f"\nSPLITT etter {h} min")
        print(f"  markeder som gjorde opp   {len(oppg):4}  markout {so:+.2f}¢"
              f"   netto {fanget + so:+.2f}¢")
        print(f"  vanlig handel             {len(vanlig):4}  markout {sv:+.2f}¢"
              f"   netto {fanget + sv:+.2f}¢")
        print(f"  -> er nettoen positiv paa vanlig handel og negativ paa oppgjor,")
        print(f"     er problemet NAERHET TIL OPPGJOR, ikke market making i seg selv.")

    print(f"\nNETTO = fanget spread + markout")
    for h, v in netto.items():
        dom = "LONNSOMT" if v > 0 else "TAP"
        print(f"  etter {h:>3} min   {v:+.2f}¢ per $100   {dom}")

    if netto:
        siste_h = max(netto)
        v = netto[siste_h]
        n = len([r for r in fylt if r.get(f"mo{siste_h}") is not None])
        xs = [r["fanget_c"] + r[f"mo{siste_h}"] for r in fylt
              if r.get(f"mo{siste_h}") is not None]
        m = snitt(xs)
        var = sum((x - m) ** 2 for x in xs) / (n - 1) if n > 1 else 0.0
        se = (var / n) ** 0.5 if n > 1 else float("inf")
        print("\n" + "=" * 62)
        if n < 30:
            print(f"SAMLER DATA — {n} fyll, trenger minst 30")
        elif m - 2 * se > 0:
            print(f"POSITIVT — {m:+.2f}¢ [{m-2*se:+.2f} – {m+2*se:+.2f}] per $100 "
                  f"etter {siste_h} min. Ugunstig utvalg spiser ikke spreaden.")
        elif m + 2 * se < 0:
            print(f"NEGATIVT — {m:+.2f}¢ [{m-2*se:+.2f} – {m+2*se:+.2f}]. Ugunstig "
                  f"utvalg er storre enn spreaden. Ikke gjor dette.")
        else:
            print(f"UAVGJORT — {m:+.2f}¢ [{m-2*se:+.2f} – {m+2*se:+.2f}]. "
                  f"Intervallet dekker null.")
        print("MERK: markout maaler IKKE oppgjor. Et binaert marked som gaar")
        print("til 0 taper hele innsatsen, og det fanges ikke av 120 minutter.")
        print("=" * 62 + "\n")



# ── Sondering: finnes handelsdata med tidsstempel? ────────────────────────
def probe_trades():
    """Bokbilder kan ikke skille en handel fra en kansellering. En market maker
    som flytter kvoten sin ser identisk ut med et fyll. Derfor ga bokmetoden
    100 % fyllrate — den talte requotes.

    Det eneste som loser det er ekte handelsprints med tidsstempel og pris:
    da vet vi at det HANDLET paa vaart nivaa, og hvor mye.

    Denne kommandoen leter etter et slikt endepunkt. Kjor én gang, si fra
    hvilken linje som gir treff."""
    m = markeder()[:1]
    if not m:
        print("fant ingen vaermarkeder aa teste mot", file=sys.stderr)
        return
    m = m[0]
    cond = m.get("conditionId")
    tid = (token_ids(m) or [None])[0]
    print(f"tester mot: {(m.get('question') or '')[:60]}")
    print(f"  conditionId {cond}\n  token {tid}\n")

    def vis(navn, url, params=None):
        d = get(url, params)
        if d is None:
            print(f"  {navn:52} FEILET/tom")
            return
        if isinstance(d, dict):
            d = d.get("data") or d.get("history") or d.get("trades") or [d]
        if not isinstance(d, list) or not d:
            print(f"  {navn:52} 0 rader")
            return
        r = d[0]
        felt = [k for k in ("price", "size", "timestamp", "t", "p", "side",
                            "match_time", "matchtime") if k in r]
        print(f"  {navn:52} {len(d):4} rader | felt: {', '.join(felt) or list(r)[:5]}")

    DATA = "https://data-api.polymarket.com"
    print("=== handelsprints ===")
    vis("data-api /trades?market=<cond>", f"{DATA}/trades",
        {"market": cond, "limit": 100})
    vis("data-api /trades?asset_id=<token>", f"{DATA}/trades",
        {"asset_id": tid, "limit": 100})
    vis("clob /trades?market=<cond>", f"{CLOB}/trades",
        {"market": cond, "limit": 100})

    print("\n=== prishistorikk (nest best) ===")
    vis("clob /prices-history 1m", f"{CLOB}/prices-history",
        {"market": tid, "interval": "1d", "fidelity": "1"})

    print("\nEt endepunkt med price + timestamp per handel loser problemet.")
    print("Bare prishistorikk holder ikke: den viser at prisen var der, ikke")
    print("at det handlet paa VAART nivaa, og ikke hvor mye.\n")



# ── Diagnose: hva inneholder handelsdataene egentlig? ─────────────────────
def probe_fill():
    """asset_id blir IGNORERT av data-api: samme ti handler kom tilbake for
    hvert token, ogsaa paa tvers av ulike markeder. Det var en global strom.

    market=<conditionId> filtrerer derimot (34 rader, ikke 100-grensen).
    Men da faar vi begge utfall i samme liste, og maa vite hvilket token hver
    handel horer til. Denne dumper ALLE feltene saa vi ser hva som finnes."""
    ms = markeder()[:2]
    if not ms:
        print("ingen markeder", file=sys.stderr)
        return
    naa = time.time()

    for m in ms:
        cond = m.get("conditionId")
        tids, outs, pr = token_ids(m), outcomes(m), _gamma_priser(m)
        print("=" * 70)
        print((m.get("question") or "")[:68])
        print(f"  priser {pr}  utfall {outs}")
        for i, t in enumerate(tids):
            print(f"  token {i}: {t}")

        d = get(f"{DATA_API}/trades", {"market": cond, "limit": 10})
        if not isinstance(d, list) or not d:
            print("\n  market=<cond> ga ingenting")
            continue

        print(f"\n  {len(d)} handler. ALLE felt i den forste:")
        for k, v in d[0].items():
            s = str(v)
            print(f"    {k:22} {s[:60]}")

        # finnes det et felt som identifiserer utfallet?
        nokler = set(d[0])
        kandidater = [k for k in nokler if any(
            o in k.lower() for o in ("asset", "outcome", "token", "side", "index"))]
        print(f"\n  mulige utfall-felt: {kandidater or 'INGEN'}")

        print(f"\n  {'alder':>9} {'pris':>7} {'storr':>8}  " +
              "  ".join(f"{k[:12]:>12}" for k in kandidater))
        for t in d[:8]:
            try:
                ts = float(t.get("timestamp") or 0)
                if ts > 1e11:
                    ts /= 1000.0
                alder = (naa - ts) / 60
                print(f"  {alder:8.1f}m {float(t['price']):>7.3f} "
                      f"{float(t.get('size') or 0):>8.1f}  " +
                      "  ".join(f"{str(t.get(k))[:12]:>12}" for k in kandidater))
            except Exception as e:
                print(f"  ukjent rad: {e}")
        print()

    print("Ser jeg et felt som matcher token-id-ene over, er problemet lost.")
    print("Finnes det ikke, kan vi ikke vite hvilket utfall en handel gjaldt,")
    print("og da lar denne maalingen seg ikke gjore med disse dataene.\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["watch", "report", "probe-trades", "probe-fill"])
    ap.add_argument("--minutes", type=int, default=0)
    ap.add_argument("--tag", default="weather",
                    help="kategori-slug: weather, crypto, sports, politics ...")
    a = ap.parse_args()
    sett_kategori(a.tag)
    print(f"kategori: {a.tag}", file=sys.stderr)
    if a.cmd == "report":
        return report()
    if a.cmd == "probe-trades":
        return probe_trades()
    if a.cmd == "probe-fill":
        return probe_fill()
    state = load_state()
    t0 = time.time()
    while True:
        try:
            sweep(state)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"! runde feilet: {e}", file=sys.stderr)
        if a.minutes and (time.time() - t0) / 60 >= a.minutes:
            break
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
