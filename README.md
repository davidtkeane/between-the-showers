# ☘️ Between the Showers

**When is the next dry gap?** That is the Irish weather question — not *will* it rain,
but when you can get out between the showers. This answers it from Met Éireann's free
forecast, and keeps an eye on an outdoor guinea pig hutch while it is at it.

No API key. No account. No cloud service.

```
  ☘️  Dublin — Met Éireann              Sat 12 Sep, 19:19

  now  17°C forecast   wind 13 km/h W · humidity 73% · 0.0mm/h
  pigs comfy  17°C — ideal range
  desk ●●●●●●○○○○ 6/10  workable — muggy

  ── dry windows ──
   ▸       now → Mon 19:00  47h  15–23°C

  ── next 24 hours ──
   temp  █▇▇▇▇▆▅▅▄▂▂▁▁▂▁▁▂▃▆▇▆▇██   15–17°C
   rain% ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁   max 6%
```

## ⚠️ Read this before relying on it

**This reads regional outdoor AIR temperature, not the temperature inside your hutch.**
A hutch in direct sun can be **10 °C or more hotter** than the air around it. Treat the
animal warnings as *"conditions are heading the wrong way, go and look"* — never as a
measurement of where your animals actually are. A €4 DS18B20 in the hutch is what turns
this from a forecast into a fact.

**It is not a vet.** The thresholds come from RSPCA and the Merck Veterinary Manual and
are cited in [`docs/WELFARE.md`](docs/WELFARE.md), but if an animal looks unwell, ring a vet.

**"Dry" means** under `0.1 mm/h` **and** under `40 %` probability — a planning threshold
for Ireland, where waiting for 0 % means never going out. Both are configurable in `.env`.
An hour with **no** forecast data is treated as **wet**, never dry: absence of data is not
good news.

## Install

```bash
git clone <this repo> && cd between-the-showers
cp .env.example .env && $EDITOR .env      # set LAT / LON / PLACE
./weather.py
```

Python 3.9+, standard library only. No dependencies.

```
./weather.py            full report
./weather.py --gaps     just the dry windows
./weather.py --puck     also push to an ESP32 display (optional)
./weather.py --watch    refresh every 10 minutes
```

## The optional display

`--puck` pushes the state to a small ESP32 screen
([RangerPuck](https://github.com/davidtkeane)) over HTTP. If the board is absent the flag
does nothing and the program carries on. You do not need it.

## Attribution — required

> Weather data: Copyright Met Éireann. Source: [met.ie](https://www.met.ie).
> This data is published under the Met Éireann Custom Open Data Licence / Creative
> Commons Attribution 4.0 International (CC BY 4.0). The data has been modified: this
> tool aggregates the raw feed into hourly dry-window and threshold summaries.
> Met Éireann does not accept any liability whatsoever for any error or omission in
> the data, their availability, or for any loss or damage arising from their use.

Met Éireann's licence also requires that anyone displaying their forecast displays their
**current warnings, unaltered**. This tool does, worst-severity first, from the official
feed at `met.ie/warningsxml/rss.xml`.

**Endpoints used — both officially published:**
- Forecast: `openaccess.pf.api.met.ie/metno-wdb2ts/locationforecast` ([data.gov.ie](https://data.gov.ie/dataset/met-eireann-forecast-api))
- Warnings: `met.ie/warningsxml/rss.xml` ([data.gov.ie](https://data.gov.ie/dataset/weather-warnings))

An earlier version of this tool read `prodapi.metweb.ie`. **Do not do that** — it is Met
Éireann's internal application backend, and they have stated publicly that it "was not
intended for public use". It was removed before first release.

## Licence

Code: MIT, see [`LICENSE`](LICENSE). Data: Met Éireann's, see above. They are separate.

## Credits

Welfare thresholds: [RSPCA UK](https://www.rspca.org.uk/adviceandwelfare/pets/rodents/guineapigs/environment),
[RSPCA Australia](https://kb.rspca.org.au/categories/companion-animals/other-pets/guinea-pigs/will-my-guinea-pigs-be-affected-by-heat),
[Merck Veterinary Manual](https://www.merckvetmanual.com/all-other-pets/guinea-pigs/unique-needs-of-guinea-pigs).

Reviewed before release by an independent model, which found four blockers including a
hardcoded home address and the use of a private API. Worth doing.
