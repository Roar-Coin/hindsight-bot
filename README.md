# Hindsight-bot

Implementerer regelen fra backtesten: kjøp krypto-favoritten første gang den handler
over 93¢, med 6t–7d til close og minst $1000 volum, hold til oppgjør.

## Oppsett

```bash
pip install py-clob-client-v2 requests
export POLYMARKET_PK=0x...        # kun nødvendig for --live
export TG_TOKEN=... TG_CHAT=...   # valgfritt varsel
```

## Kjøring

```bash
python hindsight_bot.py --once            # én skygge-runde
python hindsight_bot.py                   # løkke, tørrkjøring (standard)
python hindsight_bot.py --report          # status og målt kostnad
python hindsight_bot.py --live --stake 25 # ekte penger, krever bekreftelse
```

Kjører i praksis via GitHub Actions (`.github/workflows/bot.yml`), én runde i timen.
Tilstand og handelslogg committes tilbake til repoet.

---

# BESLUTNINGSREGELEN

**To betingelser må være oppfylt før live. Begge. Ikke én av dem.**

## 1 · Klyngegulvet må klareres  ← den bindende

Backtest per 8. august 2026 (crypto, ≥93¢, 6t–7d, min. $1000 volum):

| | |
|---|---|
| Handler | 2403 |
| Treffrate | 98.3% |
| Fordel | +2.0 pp |
| Klynger (subject + oppgjørsdag) | 166 |
| Gulv ved full korrelasjon | ±2.9 pp |
| **Dom** | **Fordelen klarer ikke gulvet** |

2403 handler er ikke 2403 uavhengige tester. To kryptomarkeder som gjøres opp
samme dag på samme mynt er ett markedsutslag talt to ganger. Ved full korrelasjon
innen klynge er utvalget effektivt 111 veddemål, og da er +2.0 pp ikke til å skille
fra flaks.

**Hva som skal til:** gulvet faller med kvadratroten av klyngeantallet. For å
presse ±2.9 under 2.0 trengs ~340 klynger. Ved ~28 nye klynger i uka er det
**rundt seks uker fra 8. august**, altså midten av september. Kommer fordelen
selv opp mot andre halvdels +2.6, går det raskere.

Klyngeantallet er den knappe ressursen, ikke handelsantallet. Dager kommer
én per døgn uansett hvor mange markeder som fyller dem.

## 2 · Gjennomføringskostnaden må holde

| Kostnad | Konsekvens |
|---|---|
| 0.5¢ | backtestens antakelse |
| 1.71¢ | fordelen dør — over dette taper du penger |

Backtesten kan ikke si hva *du* faktisk klarer å kjøpe til. Boten måler det i
tørrkjøring. Krev minst 100 målinger før du leser snittet.

Status 8. august: 39 målinger, snitt godt under taket. Ser lovende ut, men
39 er ikke 100.

## Rekkefølgen

1. La boten gå. Den samler kostnadsdata av seg selv.
2. Kjør backtesten på nytt om noen uker og se på klyngegulvet.
3. Klareres gulvet **og** kostnaden holder → sett `cost_measurement_mode = False`,
   still takene tilbake til live-verdiene i `Config`, og start på **$25 per handel**.
4. Klareres ikke gulvet → vent. Det er ikke noe å fikse i koden.

Boten nekter å kjøre `--live` så lenge `cost_measurement_mode` er på.

---

## Hva boten ikke gjør

- Ingen market orders. Marketable limit FOK med tak på VWAP-prisen.
  Uteblitt fyll er et riktig utfall, ikke en feil.
- Ingen exit. Regelen er hold-til-oppgjør; alt annet er en annen strategi.
- Ingen skalering av innsats etter vinnere. Verste handel var −100%.
- Ingen automatisk redeem.

## Stoppkriterier (avtalt på forhånd — ikke endre dem underveis)

- 3 tap på rad (backtestens verste rekke var 2)
- $200 drawdown
- 5 tap på de første 60 handlene

## Kjent svakhet i grunnlaget

Arkivet dekker 2026-06-25 → 2026-08-05. Seks uker, ett kryptoregime. Max drawdown
var $630 mot flate $100-innsatser — 87% av toppen. Du kan ligge under vann lenge
før dette virker.
