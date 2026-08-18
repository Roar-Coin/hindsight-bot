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
REQ_PAUSE = 0.15

RUN = os.environ.get("GITHUB_RUN_ID") or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
LOG_DIR, STATE_DIR = "maker", "maker-state"
LOG = os.path.join(LOG_DIR, f"{RUN}.jsonl")
STATE = os.path.join(STATE_DIR, f"{RUN}.json")


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
def markeder(tag=WEATHER_TAG):
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
            # Fyll leses av BOKEN, ikke av last-trade-price. Den siste handelen
            # kan ha skjedd for vi la ordren — ligger budet paa 47¢ fordi noen
            # solgte der i gaar, ga den gamle testen "fyll" med en gang, og
            # fyllraten ble 100 %.
            #
            # Vi laa BAKERST i koen paa vaart nivaa, med `foran_i_ko` i $ foran
            # oss. To tolkninger:
            #   konservativ  — hele nivaaet er borte (beste bud har falt under
            #                  vaar pris). Da er alt foran oss tatt, og vi ogsaa.
            #   optimistisk  — nivaaet staar, men storrelsen er mindre enn det
            #                  som laa foran oss. Noe er spist; kanskje oss.
            # Begge er tilnaermelser: en kansellering ser ut som en handel i
            # boken. Derfor overvurderer selv den konservative litt.
            bb, ba, storrelse = topp(tid)
            time.sleep(REQ_PAUSE)
            if bb is None:
                continue

            if bb < q["bud"] - 1e-9:
                q["fylt"], q["type"] = naa, "konservativ"
            elif abs(bb - q["bud"]) < 1e-9 and storrelse < q["foran_i_ko"] - 1e-9:
                q["fylt"], q["type"] = naa, "optimistisk"
                q["spist"] = round(q["foran_i_ko"] - storrelse, 2)
            else:
                # Beste bud har gaatt OPP: noen la seg foran oss. Vi ligger
                # fortsatt der, bare lenger bak. Det er ikke et fyll.
                if naa - q["lagt"] > 6 * 3600:
                    skriv({**q, "tid": tid, "utfall": "aldri_fylt"})
                    ferdige.append(tid)
                continue
            q["mid_ved_fyll"] = midt(tid)
            time.sleep(REQ_PAUSE)
            continue

        # fylt — mål markout ved hver horisont
        alder = (naa - q["fylt"]) / 60
        for h in MARKOUTS:
            n = f"mo{h}"
            if n not in q and alder >= h:
                m = midt(tid)
                time.sleep(REQ_PAUSE)
                # positiv = prisen gikk VAAR vei etter at vi kjopte
                q[n] = round((m - q["bud"]) * 100, 3) if m is not None else None

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
            bb, ba, foran = topp(tid)
            time.sleep(REQ_PAUSE)
            if bb is None or ba - bb < MIN_SPREAD:
                continue
            kv[tid] = {
                "lagt": naa, "bud": bb, "ask": ba,
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
    if fylt and len(fylt) / len(rows) > 0.5:
        print("  !! fyllrate over 50 % er urimelig hoyt for en hvilende ordre.")
        print("     Sjekk fylldeteksjonen for du stoler paa tallene under.")
    kons = sum(1 for r in fylt if r.get("type") == "konservativ")
    print(f"  herav konservative fyll {kons} / optimistiske {len(fylt)-kons}")

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["watch", "report", "probe-trades"])
    ap.add_argument("--minutes", type=int, default=0)
    a = ap.parse_args()
    if a.cmd == "report":
        return report()
    if a.cmd == "probe-trades":
        return probe_trades()
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
