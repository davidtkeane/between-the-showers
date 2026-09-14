#!/usr/bin/env python3
"""
Ranger Weather — Met Éireann, built around the actual Irish question:
NOT "will it rain" but "WHEN IS THE NEXT DRY GAP so I can go out."

Sources, all free, no API key:
  forecast     openaccess.pf.api.met.ie/metno-wdb2ts/locationforecast  (hourly, 10 days)
  observations prodapi.metweb.ie/observations/dublin/today
  warnings     prodapi.metweb.ie/warnings/warnings                     (yellow/orange/red)
  geocode      prodapi.metweb.ie/location/reverse/LAT/LON

Also watches the guinea pig hutch against sourced welfare thresholds — docs/WELFARE.md.

  ./weather.py              full report
  ./weather.py --gaps       just the dry windows
  ./weather.py --puck       push to the RangerPuck
  ./weather.py --watch      refresh every 10 min, push to the puck
"""
import sys, os, json, time, subprocess, urllib.request, urllib.parse, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PUCK = Path.home() / "esp32-projects/1-ranger-puck/tools/send.sh"
def _env(name, default):
    """Read from .env if present, else the default. Keeps the author's actual
    location OUT of the source — see README. Default is Dublin city centre."""
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{name}=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default

LAT  = float(_env("LAT", 53.3498))           # default: Dublin city centre
LON  = float(_env("LON", -6.2603))
WET_MM   = float(_env("WET_MM", 0.1))        # mm/h at or above which an hour is "wet"
WET_PROB = float(_env("WET_PROB", 40))       # % probability at or above which it is "wet"
GP_COLD, GP_CHILL, GP_WARM, GP_HOT = 15, 17, 23, 26   # matches docs/WELFARE.md exactly

C = dict(r='\033[31m', g='\033[32m', y='\033[33m', b='\033[34m', c='\033[36m',
         m='\033[35m', d='\033[90m', B='\033[1m', N='\033[0m')

def fetch_json(url, timeout=12):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r: return json.load(r)
    except Exception: return None

def sun_times(lat, lon, when=None):
    """Sunrise and sunset in UTC — simplified NOAA algorithm, no dependencies.

    Needed because "sunny at 3am" is not a useful thing to tell anybody. Accurate
    to a couple of minutes, which is plenty for "is it worth going outside".
    """
    import math
    d = (when or datetime.now(timezone.utc)).date()
    n = d.toordinal() - datetime(2000, 1, 1).date().toordinal() + 0.0008
    J = n - lon / 360.0
    M = (357.5291 + 0.98560028 * J) % 360
    Cc = 1.9148*math.sin(math.radians(M)) + 0.02*math.sin(math.radians(2*M)) \
         + 0.0003*math.sin(math.radians(3*M))
    L = (M + Cc + 180 + 102.9372) % 360
    Jt = 2451545.0 + J + 0.0053*math.sin(math.radians(M)) - 0.0069*math.sin(math.radians(2*L))
    dec = math.asin(math.sin(math.radians(L)) * math.sin(math.radians(23.44)))
    try:
        w = math.acos((math.sin(math.radians(-0.833)) - math.sin(math.radians(lat))*math.sin(dec))
                      / (math.cos(math.radians(lat))*math.cos(dec)))
    except ValueError:
        return None, None                     # polar day or night
    rise = Jt - math.degrees(w)/360.0
    setj = Jt + math.degrees(w)/360.0
    to_dt = lambda j: datetime(2000,1,1,12,tzinfo=timezone.utc) + timedelta(days=j-2451545.0)
    return to_dt(rise), to_dt(setj)

def sun_windows(rows, max_cloud=45, min_len=2):
    """Runs of bright daylight hours — the other half of the Irish question.

    Knowing when the sun is coming matters as much as knowing when the rain is,
    and no weather app tells you. Uses Met Éireann's cloudiness percentage, and
    only counts hours between sunrise and sunset.
    """
    if not rows: return []
    rise, set_ = sun_times(LAT, LON)
    if not rise: return []
    out, cur = [], []
    for r in rows:
        day = rise.time() <= r["t"].time() <= set_.time() if rise.date() == r["t"].date() \
              else 6 <= r["t"].astimezone().hour <= 20     # crude for later days
        bright = day and r.get("cloudiness", 100) <= max_cloud and not r["wet"]
        cont = (not cur) or (r["t"] - cur[-1]["t"] == timedelta(hours=1))
        if bright and cont: cur.append(r)
        else:
            if len(cur) >= min_len: out.append(cur)
            cur = [r] if bright else []
    if len(cur) >= min_len: out.append(cur)
    return out

def rain_minutes():
    """Minute-by-minute rain for the next hour — the 5-minute warning.

    OPTIONAL. Met Éireann's forecast is hourly and cannot do this, so it uses
    OpenWeather One Call 3.0, which needs a key. Without one this returns None
    and everything else still works: the core tool stays keyless.
    Returns (minutes_until_rain, minutes_until_it_stops) or None.
    """
    key = _env("OPENWEATHER_API_KEY", "")
    if not key: return None
    try:
        q = urllib.parse.urlencode({"lat": LAT, "lon": LON, "units": "metric",
                                    "exclude": "daily,alerts", "appid": key})
        with urllib.request.urlopen(
                f"https://api.openweathermap.org/data/3.0/onecall?{q}", timeout=12) as r:
            d = json.load(r)
    except Exception:
        return None
    mins = d.get("minutely") or []
    if not mins: return None
    wet = [i for i, m in enumerate(mins) if m.get("precipitation", 0) > 0]
    if not wet:
        return ("dry", len(mins))
    start = wet[0]
    stop = next((i for i in range(start, len(mins))
                 if mins[i].get("precipitation", 0) == 0), len(mins))
    return ("rain", start, stop)

def warnings_rss():
    """Met Éireann's OFFICIAL warnings feed.

    The previous version read prodapi.metweb.ie, which is Met Éireann's INTERNAL
    app backend — they have stated publicly it "was not intended for public use".
    This is the published one, and the licence REQUIRES that anyone displaying
    their forecast also displays current warnings, unaltered.
    """
    try:
        raw = urllib.request.urlopen("https://www.met.ie/warningsxml/rss.xml", timeout=12).read()
        x = ET.fromstring(raw)
        out = []
        for item in x.iter("item"):
            title = (item.findtext("title") or "").strip()
            desc  = (item.findtext("description") or "").strip()
            lvl = ("Red" if "red" in title.lower() else
                   "Orange" if "orange" in title.lower() else
                   "Yellow" if "yellow" in title.lower() else None)
            if lvl: out.append({"level": lvl, "headline": title, "description": desc})
        # licence condition: never drop a worse warning to show a milder one
        rank = {"Red": 0, "Orange": 1, "Yellow": 2}
        return sorted(out, key=lambda w: rank[w["level"]])
    except Exception:
        return []

def forecast():
    """Raises nothing the caller cannot handle — see report()."""
    url = f"http://openaccess.pf.api.met.ie/metno-wdb2ts/locationforecast?lat={LAT};long={LON}"
    x = ET.fromstring(urllib.request.urlopen(url, timeout=25).read())
    inst, per = {}, {}
    for t in x.iter("time"):
        f = datetime.fromisoformat(t.get("from").replace("Z", "+00:00"))
        to = datetime.fromisoformat(t.get("to").replace("Z", "+00:00"))
        if f == to:                                   # instant values
            d = inst.setdefault(f, {})
            for tag, attr in (("temperature","value"), ("humidity","value"),
                              ("windSpeed","mps"), ("windGust","mps"), ("cloudiness","percent"),
                              ("pressure","value")):
                e = t.find(f".//{tag}")
                if e is not None and e.get(attr): d[tag] = float(e.get(attr))
            e = t.find(".//windDirection")
            if e is not None: d["windDir"] = e.get("name", "")
            e = t.find(".//symbol")
            if e is not None: d["symbol"] = e.get("id", "")
        else:                                          # period values
            p = t.find(".//precipitation")
            if p is not None:
                per[f] = {"mm": float(p.get("value", 0)),
                          "maxmm": float(p.get("maxvalue", p.get("value", 0))),
                          "prob": float(p.get("probability", 0))}
    return inst, per

def hourly(inst, per, hours=48):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    out = []
    for i in range(hours):
        t = now + timedelta(hours=i)
        if t not in inst: continue
        r = dict(inst[t]); r["t"] = t
        p = per.get(t, {})
        # An hour with NO precipitation record is UNKNOWN, not dry. For a tool
        # whose only job is "when is it safe to go out", absence of data must
        # never read as good news.
        if not p:
            r["known"] = False
            r["mm"] = r["maxmm"] = r["prob"] = None
            r["wet"] = True                      # fail closed
        else:
            r["known"] = True
            r["mm"], r["prob"] = p.get("mm", 0.0), p.get("prob", 0.0)
            r["maxmm"] = p.get("maxmm", r["mm"])
            r["wet"] = r["mm"] >= WET_MM or r["prob"] >= WET_PROB
        out.append(r)
    return out

def dry_gaps(rows, min_len=2):
    """Runs of consecutive dry hours — the thing you actually plan around.

    A run is only valid if the hours are GENUINELY CONSECUTIVE. The forecast can
    thin out to 3-hourly further ahead; without this check a "6h window" could
    span 18 real hours with untested wet ones inside it.
    """
    gaps, cur = [], []
    for r in rows:
        gap_ok = (not cur) or (r["t"] - cur[-1]["t"] == timedelta(hours=1))
        if r["wet"] or not r.get("known", False) or not gap_ok:
            if len(cur) >= min_len: gaps.append(cur)
            cur = [r] if (not r["wet"] and r.get("known") and not gap_ok) else []
        else:
            cur.append(r)
    if len(cur) >= min_len: gaps.append(cur)
    return gaps

def fmt1(v, unit="", nd=1):
    """Format a value that may be None. hourly() deliberately yields None for an
    hour with no precipitation record, and three separate consumers formatted it
    straight into an f-string. Unknown prints as "--", never as 0."""
    return f"--{unit}" if v is None else f"{v:.{nd}f}{unit}"

def known(vals):
    """Drop unknown hours before any min/max/arithmetic. max() over a list
    containing None raises TypeError, which took the whole 24h graph down."""
    return [v for v in vals if v is not None]

def spark(vals, lo=None, hi=None):
    if not vals: return ""
    blocks = "▁▂▃▄▅▆▇█"
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    if hi - lo < 0.01: hi = lo + 1
    return "".join(blocks[min(7, max(0, int((v - lo) / (hi - lo) * 7.99)))] for v in vals)

def gp_state(t):
    if t >= GP_HOT:    return "DANGER", f"{C['r']}TOO HOT{C['N']}",  "heatstroke risk"
    if t >= GP_WARM:   return "WARN",   f"{C['y']}warm{C['N']}",     "hyperthermia possible from 24"
    if t <  GP_COLD:   return "DANGER", f"{C['r']}TOO COLD{C['N']}", "bring them in (RSPCA)"
    if t <  GP_CHILL:  return "WARN",   f"{C['y']}chilly{C['N']}",   "extra bedding"
    return "OK", f"{C['g']}comfy{C['N']}", "ideal range"

def study_score(temp, humidity, pressure=None):
    """How good are conditions for sitting down and writing?

    A rough heuristic, NOT science. Temperature/performance has some support in the
    office-environment literature (e.g. Seppanen et al. on thermal conditions and
    office work), but the humidity and pressure terms here are rules of thumb, not
    findings. It is a nudge to open a window, nothing more.
    """
    score, notes = 10, []
    if   20 <= temp <= 22: pass
    elif 16 <= temp < 18 or 22 < temp <= 24: score -= 2; notes.append("temp a bit off")
    else: score -= 4; notes.append("temp will distract you")
    if humidity > 75: score -= 2; notes.append("muggy")
    elif humidity > 65: score -= 1; notes.append("close")
    if pressure and pressure < 1000: score -= 1; notes.append("low pressure")
    score = max(1, min(10, score))
    verdict = ("good writing weather" if score >= 8 else
               "workable" if score >= 6 else
               "open a window first")
    return score, verdict, notes

def to_puck_weather(temp, cond, rain, pigs, place):
    """Push the ambient weather screen. NOT a state — weather is not urgent and
    should not compete with an agent for the display. It takes its turn in the
    board's ambient rotation alongside the planes and the fleet."""
    import urllib.request
    cache = Path.home() / ".ranger-memory/config/rangerpuck.ip"
    host = cache.read_text().strip() if cache.exists() else "rangerpuck.local"
    body = json.dumps({"temp": temp, "cond": cond, "rain": rain,
                       "pigs": pigs, "place": place}).encode()
    for h in (host, "rangerpuck.local"):
        try:
            req = urllib.request.Request(f"http://{h}/weather", data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=3).read()
            return True
        except Exception:
            continue
    return False

def to_puck(state, l1, l2):
    """Optional ESP32 display. Never blocks, never raises, no shell."""
    if not PUCK.exists(): return
    try:
        subprocess.Popen([str(PUCK), state, l1, l2, "WEATHER"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def report(push=False, gaps_only=False):
    try:
        inst, per = forecast()
    except Exception as e:
        print(f"\n  {C['r']}Met Éireann forecast unavailable{C['N']} {C['d']}({type(e).__name__}){C['N']}")
        print(f"  {C['d']}no data means no advice — not 'it is fine'.{C['N']}\n")
        to_puck("ERROR", "no forecast", "met.ie down")
        return
    rows = hourly(inst, per, 48)
    if not rows: print("  no forecast data"); return
    obs = None                       # see README: no official per-location obs feed
    warn = warnings_rss()
    place = _env("PLACE", "your location")

    now = rows[0]
    gaps = dry_gaps(rows)

    if not gaps_only:
        print(f"\n{C['B']}{C['c']}  ☘️  {place} — Met Éireann{C['N']}"
              f"{C['d']}   {datetime.now().strftime('%a %d %b, %H:%M')}{C['N']}\n")
        # one temperature only, clearly labelled as forecast — see README.
        print(f"  {C['B']}now{C['N']}  {now['temperature']:.0f}°C forecast"
              f"   {C['d']}wind {now.get('windSpeed',0)*3.6:.0f} km/h {now.get('windDir','')}"
              f" · humidity {now.get('humidity',0):.0f}%"
              f" · {fmt1(now['mm'], 'mm/h')}{C['N']}")
        sev, label, note = gp_state(now["temperature"])
        print(f"  {C['B']}pigs{C['N']} {label}  {C['d']}{now['temperature']:.0f}°C — {note}{C['N']}")

        sc, verdict, notes = study_score(now["temperature"], now.get("humidity", 60),
                                         now.get("pressure"))
        bar = "●" * sc + f"{C['d']}○{C['N']}" * (10 - sc)
        col = C['g'] if sc >= 8 else C['y'] if sc >= 6 else C['r']
        print(f"  {C['B']}desk{C['N']} {col}{bar}{C['N']} {sc}/10  {C['d']}{verdict}"
              f"{(' — ' + ', '.join(notes)) if notes else ''}{C['N']}")

        # official warnings, worst first
        #
        # warnings_rss() returns a LIST. This block consumed the old prodapi
        # DICT shape and raised AttributeError on ANY live warning — i.e. on
        # exactly the cold or stormy days when the guinea pig alert matters,
        # and the scheduled job swallowed the traceback into /dev/null. The
        # licence-compliance refactor changed the producer and left the consumer.
        #
        # Met Éireann's licence REQUIRES current warnings be shown unaltered,
        # so they are already sorted worst-first before any truncation.
        for w in (warn or [])[:4]:
            col = {"Yellow": C['y'], "Orange": C['m'], "Red": C['r']}.get(w.get("level"), "")
            print(f"  {col}⚠️  {w.get('headline','')}{C['N']}")

    # ---- the next hour, minute by minute (optional, needs a key) ----
    # OpenWeather's minutely feed disagreed with Met Éireann on 2026-09-12 — it
    # reported "raining now" on a dry evening. Met Éireann is the national service
    # and the primary source here, so the minute feed is only allowed to SHOUT when
    # the hourly forecast agrees rain is plausible. Otherwise it is shown as an
    # unconfirmed second opinion. Same principle as amber on the puck: a warning
    # that cries wolf trains you to ignore it.
    rm = rain_minutes()
    met_says_possible = now["prob"] is not None and now["prob"] >= 15
    if rm and not gaps_only:
        if rm[0] == "rain":
            start, stop = rm[1], rm[2]
            dur = stop - start
            if not met_says_possible:
                print(f"\n  {C['d']}· OpenWeather's minute feed says rain"
                      f"{' now' if start == 0 else f' in {start}m'}, but Met Éireann has"
                      f" it at {now['prob']:.0f}% — treating as unconfirmed.{C['N']}")
                to_puck_later = None
            elif start == 0:
                print(f"\n  {C['b']}{C['B']}🌧️  RAINING NOW{C['N']}"
                      f"  {C['d']}eases in about {stop} min{C['N']}")
                to_puck_later = ("RAIN", "raining now", f"eases {stop}m")
            else:
                urgency = C['r'] if start <= 10 else C['y'] if start <= 25 else C['d']
                print(f"\n  {urgency}{C['B']}🌧️  RAIN IN {start} MINUTES{C['N']}"
                      f"  {C['d']}lasting about {dur} min — get the washing in{C['N']}")
                to_puck_later = ("RAIN", f"rain in {start}m", f"lasts ~{dur}m")
        else:
            print(f"\n  {C['g']}☀️  no rain in the next {rm[1]} minutes{C['N']}")
            to_puck_later = None
    else:
        to_puck_later = None

    # ---- the bit that matters: when can you go out ----
    print(f"\n{C['B']}{C['g']}  ── dry windows ──{C['N']}")
    if not gaps:
        print(f"  {C['r']}no dry gap of 2h+ in the next 48h. Irish summer.{C['N']}")
    for g in gaps[:5]:
        a, b = g[0]["t"].astimezone(), g[-1]["t"].astimezone() + timedelta(hours=1)
        hrs = len(g)
        when = "now" if g[0] is rows[0] else a.strftime("%a %H:%M")
        # show the day on the end time if the window crosses midnight
        endf = "%H:%M" if b.date() == a.date() else "%a %H:%M"
        temps = [r["temperature"] for r in g]
        note = " (to end of forecast)" if g[-1] is rows[-1] else ""
        print(f"   {C['g']}▸{C['N']} {when:>9} → {b.strftime(endf):<9} "
              f"{C['B']}{hrs}h{C['N']}  {C['d']}{min(temps):.0f}–{max(temps):.0f}°C{note}{C['N']}")

    suns = sun_windows(rows)
    print(f"\n{C['B']}{C['y']}  ── sunshine ──{C['N']}")
    rise, set_ = sun_times(LAT, LON)
    if rise:
        nowt = datetime.now(timezone.utc)
        line = (f"   {C['d']}sunrise {rise.astimezone().strftime('%H:%M')}"
                f" · sunset {set_.astimezone().strftime('%H:%M')}")
        # a countdown to whichever comes next — golden hour is worth catching
        for label, when in (("sunrise", rise), ("sunset", set_)):
            mins = int((when - nowt).total_seconds() // 60)
            if 0 < mins <= 90:
                col = C['y'] if mins <= 30 else C['d']
                line = (f"   {col}{'🌅' if label=='sunrise' else '🌇'} {label} in {mins} min"
                        f"{C['N']}{C['d']}  ·  {'sunset' if label=='sunrise' else 'sunrise'} "
                        f"{(set_ if label=='sunrise' else rise).astimezone().strftime('%H:%M')}")
                break
        print(line + C['N'])
    if not suns:
        print(f"   {C['d']}no bright spell of 2h+ forecast. Ireland.{C['N']}")
    for g in suns[:4]:
        a = g[0]["t"].astimezone(); b = g[-1]["t"].astimezone() + timedelta(hours=1)
        cl = sum(r.get("cloudiness", 0) for r in g) / len(g)
        endf = "%H:%M" if b.date() == a.date() else "%a %H:%M"
        when = "now" if g[0] is rows[0] else a.strftime("%a %H:%M")
        print(f"   {C['y']}☀{C['N']} {when:>9} → {b.strftime(endf):<9} "
              f"{C['B']}{len(g)}h{C['N']}  {C['d']}{cl:.0f}% cloud{C['N']}")

    if gaps_only: return

    # ---- 24h graphs ----
    day = rows[:24]
    temps = [r["temperature"] for r in day]
    # unknown hours are dropped for the maths and drawn as a gap, not as zero
    probs = known([r["prob"] for r in day])
    mms   = known([r["maxmm"] for r in day])
    unknown = sum(1 for r in day if not r.get("known", False))
    print(f"\n{C['B']}  ── next 24 hours ──{C['N']}")
    print(f"   temp  {C['c']}{spark(temps)}{C['N']}  {C['d']}{min(temps):.0f}–{max(temps):.0f}°C{C['N']}")
    if probs:
        print(f"   rain% {C['b']}{spark(probs, 0, 100)}{C['N']}  {C['d']}max {max(probs):.0f}%{C['N']}")
    if mms:
        print(f"   mm    {C['b']}{spark(mms, 0, max(1.0, max(mms)))}{C['N']}  {C['d']}max {max(mms):.1f}mm{C['N']}")
    if unknown:
        print(f"   {C['d']}{unknown} of the next 24 hours have no rainfall record — "
              f"counted as WET, not as dry.{C['N']}")
    print(f"   {C['d']}      {''.join((str(r['t'].astimezone().hour).rjust(1) if i%6==0 else ' ') for i,r in enumerate(day))}{C['N']}")

    lo = min(r["temperature"] for r in rows[:24])
    if lo < GP_COLD:
        print(f"\n  {C['r']}🔴 drops to {lo:.1f}°C in the next 24h — below the RSPCA 15°C line.{C['N']}")
        print(f"     {C['d']}extra bedding, out of the wind, or bring them in.{C['N']}")

    print(f"{C['d']}  \"dry\" = under {WET_MM}mm/h and under {WET_PROB:.0f}% probability."
          f"  Outdoor AIR temperature for the area — NOT inside the hutch.{C['N']}")
    print(f"{C['d']}  Data: Copyright Met Éireann (met.ie), CC BY 4.0. Modified. No liability accepted.{C['N']}")

    if push:
        # the ambient weather screen — always, regardless of state
        sev_w, label_w, _ = gp_state(now["temperature"])
        pigline = {"OK": "pigs comfy", "WARN": f"pigs {label_w.split('m')[-1].strip() or 'watch'}",
                   "DANGER": "PIGS: " + ("TOO HOT" if now["temperature"] >= GP_HOT else "TOO COLD")}[sev_w]
        import re as _re
        pigline = _re.sub(r'\x1b\[[0-9;]*m', '', pigline)
        to_puck_weather(f"{now['temperature']:.0f}C",
                        (now.get("symbol") or "").replace("_", " ")[:12] or
                        ("rain" if now["prob"] and now["prob"] >= 40 else "dry"),
                        f"rain {now['prob']:.0f}%" if now["prob"] is not None else "",
                        pigline, _env("PLACE", "weather"))

        # ONLY the welfare alert becomes a STATE. Weather already has its own
        # ambient screen (pushed above), and sending DRY/SUN/RAIN as states too
        # meant the rotation showed weather TWICE — once as the proper screen and
        # once as a state card. Two mechanisms doing one job.
        #
        # PIGS is different: it is an alert, not information. Cold enough to hurt
        # an animal should interrupt, not wait its turn.
        t = now["temperature"]
        sev, _, _ = gp_state(t)
        if sev == "DANGER":
            to_puck("PIGS", f"{t:.0f}C {'TOO HOT' if t >= GP_HOT else 'TOO COLD'}",
                    "check the hutch")
    print()

# ─── THE PIG RUN ────────────────────────────────────────────────────────────
# The most useful thing this whole tool does. David has to walk a few hundred
# yards to the guinea pigs, feed them and spend ~half an hour checking them —
# call it a 45-minute round trip. The question is never "will it rain today", it
# is "do I have a long-enough DRY GAP right now to do the run and get back".
#
# Two data horizons, stitched together:
#   0-60 min : OpenWeather minute nowcast (rain_minutes) — sharp, radar-grade
#   60 min+  : Met Eireann hourly dry_gaps() — for planning ahead
def pig_run(run_min=45):
    """Returns a dict the terminal AND the puck can render. status is GO or WAIT."""
    now = datetime.now(timezone.utc)
    rm = rain_minutes()                     # None | ('dry',N) | ('rain',start,stop)
    try:
        inst, per = forecast(); rows = hourly(inst, per, 48)
    except Exception:
        rows = []
    gaps = dry_gaps(rows) if rows else []

    def gap_bounds(g): return g[0]["t"], g[-1]["t"] + timedelta(hours=1)
    def hourly_dry_left():                  # dry minutes left in the hourly gap we're in
        for g in gaps:
            s, e = gap_bounds(g)
            if s <= now < e: return int((e - now).total_seconds() // 60)
        return 0
    def next_gap():                         # next gap >= run_min starting after now
        for g in gaps:
            s, e = gap_bounds(g)
            if s > now and (e - s).total_seconds() // 60 >= run_min:
                return s.astimezone(), int((e - s).total_seconds() // 60)
        return None, None

    rise, sett = sun_times(LAT, LON)
    dark_in = int((sett - now).total_seconds() // 60) if sett and sett > now else -1

    st = dict(status="WAIT", head="", sub="", rain_left=None, dry_left=None,
              next_at=None, next_len=None, dark_in=dark_in, run_min=run_min)

    # --- decide -------------------------------------------------------------
    if rm and rm[0] == "rain" and rm[1] == 0:
        # raining right now
        st["rain_left"] = rm[2]
        left = f"{rm[2]}+min" if rm[2] >= 60 else f"{rm[2]}min"
        st.update(status="WAIT", head=f"WAIT · rain {left} left")
        na, nl = next_gap()
        if na: st.update(next_at=na, next_len=nl, sub=f"next GO {na:%H:%M} ({nl//60}h{nl%60:02d})")
        else:  st["sub"] = "no clear window in forecast"
    elif rm and rm[0] == "rain" and rm[1] > 0:
        # dry now, rain starts in rm[1] min
        st["dry_left"] = rm[1]
        if rm[1] >= run_min:
            st.update(status="GO", head=f"GO · dry {rm[1]}min", sub=f"rain after that ({rm[2]-rm[1]}min shower)")
        else:
            st.update(status="WAIT", head=f"WAIT · rain in {rm[1]}min",
                      sub=f"only {rm[1]}m dry, need {run_min}m")
    else:
        # dry now (minute says dry, or no minute data → trust hourly)
        minute_dry = rm[1] if (rm and rm[0] == "dry") else 0
        dry_left = max(minute_dry, hourly_dry_left())
        st["dry_left"] = dry_left
        if dry_left >= run_min:
            disp = f"{dry_left//60}h{dry_left%60:02d}" if dry_left >= 60 else f"{dry_left}min"
            st.update(status="GO", head=f"GO · dry {disp}")
        else:
            st.update(status="WAIT", head=f"WAIT · dry only {dry_left}min")
            na, nl = next_gap()
            if na: st.update(next_at=na, next_len=nl, sub=f"next GO {na:%H:%M}")

    # darkness note — a 45-min run into the dark needs a torch
    if st["status"] == "GO" and 0 <= dark_in <= run_min:
        st["sub"] = (st["sub"] + " · " if st["sub"] else "") + f"DARK in {dark_in}min — torch"
    return st

def print_pig_run(run_min=45):
    s = pig_run(run_min)
    col = C['g'] if s["status"] == "GO" else C['r']
    icon = "🟢" if s["status"] == "GO" else "🔴"
    print(f"\n  {icon} {C['B']}{col}PIG RUN: {s['head']}{C['N']}  {C['d']}(run = {run_min}min){C['N']}")
    if s["sub"]: print(f"     {C['d']}{s['sub']}{C['N']}")
    print()


def pig_push(run_min=45):
    """Compute the pig-run status and push the matching state to the puck. One-shot,
    called by launchd every few minutes. GO=green, WAIT=red, SOON=pulsing red."""
    s = pig_run(run_min)
    if s["status"] == "GO":
        st = "PIGGO"
    elif s.get("dry_left") is not None and 0 < s["dry_left"] <= 10:
        st = "PIGSOON"                       # dry now, rain within 10 min — the flash
    else:
        st = "PIGWAIT"
    l1 = s["head"].replace("GO \u00b7 ", "").replace("WAIT \u00b7 ", "")[:14]
    l2 = (s["sub"] or "")[:20]
    send = str(Path.home() / "esp32-projects/1-ranger-puck/tools/send.sh")
    if os.path.exists(send):
        env = dict(os.environ, PUCK_WHO="PIGS")
        subprocess.Popen([send, st, l1, l2], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return st, l1, l2


if __name__ == "__main__":
    if "--watch" in sys.argv:
        while True:
            try: report(push=True)
            except Exception as e: print(f"  error: {e}")
            time.sleep(600)
    elif "--pigrun" in sys.argv or "--pig" in sys.argv:
        print_pig_run(45)
    elif "--pigpush" in sys.argv:
        st, l1, l2 = pig_push(45); print(f"  pushed {st}: {l1} | {l2}")
    else:
        report(push="--puck" in sys.argv, gaps_only="--gaps" in sys.argv)
