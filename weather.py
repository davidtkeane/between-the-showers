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
import sys, os, json, time, subprocess, urllib.request, xml.etree.ElementTree as ET
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
              f" · {now['mm']:.1f}mm/h{C['N']}")
        sev, label, note = gp_state(now["temperature"])
        print(f"  {C['B']}pigs{C['N']} {label}  {C['d']}{now['temperature']:.0f}°C — {note}{C['N']}")

        sc, verdict, notes = study_score(now["temperature"], now.get("humidity", 60),
                                         now.get("pressure"))
        bar = "●" * sc + f"{C['d']}○{C['N']}" * (10 - sc)
        col = C['g'] if sc >= 8 else C['y'] if sc >= 6 else C['r']
        print(f"  {C['B']}desk{C['N']} {col}{bar}{C['N']} {sc}/10  {C['d']}{verdict}"
              f"{(' — ' + ', '.join(notes)) if notes else ''}{C['N']}")

        # official warnings for Dublin
        if warn:
            # marine warnings (gales, small craft) are irrelevant 10km inland — skip them
            cats = [c for k, c in warn.get("warnings", {}).items()
                    if isinstance(c, list) and k != "marine"]
            live = [w for cat in cats for w in cat
                    if isinstance(w, dict) and w.get("level") in ("Yellow","Orange","Red")
                    and (not w.get("regions") or "EI06" in w.get("regions", [])
                         or any("Dublin" in str(r) for r in w.get("regions", [])))]
            for w in live[:3]:
                col = {"Yellow": C['y'], "Orange": C['m'], "Red": C['r']}.get(w["level"], "")
                print(f"  {col}⚠️  {w['level']}: {w.get('headline','')}{C['N']}")

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

    if gaps_only: return

    # ---- 24h graphs ----
    day = rows[:24]
    temps = [r["temperature"] for r in day]
    probs = [r["prob"] for r in day]
    mms   = [r["maxmm"] for r in day]
    print(f"\n{C['B']}  ── next 24 hours ──{C['N']}")
    print(f"   temp  {C['c']}{spark(temps)}{C['N']}  {C['d']}{min(temps):.0f}–{max(temps):.0f}°C{C['N']}")
    print(f"   rain% {C['b']}{spark(probs, 0, 100)}{C['N']}  {C['d']}max {max(probs):.0f}%{C['N']}")
    print(f"   mm    {C['b']}{spark(mms, 0, max(1.0, max(mms)))}{C['N']}  {C['d']}max {max(mms):.1f}mm{C['N']}")
    hrs = "".join(f"{r['t'].astimezone().hour%24:<1}" if i%3==0 else " " for i,r in enumerate(day))
    print(f"   {C['d']}      {''.join((str(r['t'].astimezone().hour).rjust(1) if i%6==0 else ' ') for i,r in enumerate(day))}{C['N']}")

    lo = min(r["temperature"] for r in rows[:24])
    if lo < GP_COLD:
        print(f"\n  {C['r']}🔴 drops to {lo:.1f}°C in the next 24h — below the RSPCA 15°C line.{C['N']}")
        print(f"     {C['d']}extra bedding, out of the wind, or bring them in.{C['N']}")

    print(f"{C['d']}  \"dry\" = under {WET_MM}mm/h and under {WET_PROB:.0f}% probability."
          f"  Outdoor AIR temperature for the area — NOT inside the hutch.{C['N']}")
    print(f"{C['d']}  Data: Copyright Met Éireann (met.ie), CC BY 4.0. Modified. No liability accepted.{C['N']}")

    if push:
        t = now["temperature"]
        sev, _, _ = gp_state(t)
        if sev == "DANGER":
            # the pigs outrank the weather — this is the one that can do harm
            to_puck("PIGS", f"{t:.0f}C {'TOO HOT' if t >= GP_HOT else 'TOO COLD'}",
                    "check the hutch")
        elif gaps and gaps[0][0] is rows[0]:
            b = gaps[0][-1]["t"].astimezone() + timedelta(hours=1)
            hrs = len(gaps[0])
            label = f"dry {hrs}h" if hrs < 24 else "dry all day"
            to_puck("DRY", label, f"{t:.0f}C til {b.strftime('%H:%M')}")
        else:
            nxt = gaps[0][0]["t"].astimezone().strftime("%H:%M") if gaps else "--"
            to_puck("RAIN", f"dry at {nxt}", f"{t:.0f}C  {now['prob']:.0f}% now")
    print()

if __name__ == "__main__":
    if "--watch" in sys.argv:
        while True:
            try: report(push=True)
            except Exception as e: print(f"  error: {e}")
            time.sleep(600)
    else:
        report(push="--puck" in sys.argv, gaps_only="--gaps" in sys.argv)
