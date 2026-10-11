"""Fetch surf data for Campus Point and write data.json.
Units: wave height ft, wave period s, wind kt, temperature F, tide ft (MLLW)."""
import json, sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests

PT = ZoneInfo("America/Los_Angeles")
UA = {"User-Agent": "campus-point-surf (ecseale@gmail.com)"}  # <- put your email
BUOY, TIDE_STATION, NWS_STATION = "46054", "9411340", "KSBA"
M_TO_FT, MS_TO_KT, KMH_TO_KT = 3.28084, 1.94384, 0.539957
COOPS = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
SPOT_SEA = (34.38, -119.85)    # just offshore of Campus Point (marine model needs an ocean cell)
SPOT_LAND = (34.41, -119.85)   # UCSB (wind model)


def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=25, **kw)
    r.raise_for_status()
    return r


def num(s):
    try:
        return None if s in ("MM", "", None) else float(s)
    except ValueError:
        return None


def text(s):
    return None if s in ("MM", "", None) else s


def mul(v, k, nd=1):
    return None if v is None else round(v * k, nd)


def ndbc_rows(ext):
    lines = get(f"https://www.ndbc.noaa.gov/data/realtime2/{BUOY}.{ext}").text.splitlines()
    cols = lines[0].lstrip("#").split()
    return [dict(zip(cols, l.split())) for l in lines[2:14]]  # newest first


def row_time(r):
    return datetime(int(r["YY"]), int(r["MM"]), int(r["DD"]), int(r["hh"]), int(r["mm"]),
                    tzinfo=timezone.utc).isoformat()


def pick(rows, key, conv=num):
    """Newest non-missing value for a field (NDBC rows often have 'MM' gaps)."""
    for r in rows:
        v = conv(r.get(key))
        if v is not None:
            return v, row_time(r)
    return None, None


def buoy():
    std, spec = ndbc_rows("txt"), ndbc_rows("spec")
    hs, t_wave = pick(std, "WVHT")
    dpd, _ = pick(std, "DPD")
    mwd, _ = pick(std, "MWD")
    ws, t_wind = pick(std, "WSPD")
    gst, _ = pick(std, "GST")
    wdir, _ = pick(std, "WDIR")
    wtmp, t_wt = pick(std, "WTMP")
    swh, t_sw = pick(spec, "SwH")
    swp, _ = pick(spec, "SwP")
    swd, _ = pick(spec, "SwD", text)
    wwh, _ = pick(spec, "WWH")
    wwp, _ = pick(spec, "WWP")
    wwd, _ = pick(spec, "WWD", text)
    return {
        "station": BUOY,
        "wave": {"height_ft": mul(hs, M_TO_FT), "period_s": dpd, "dir_deg": mwd, "time": t_wave},
        "swell": {"height_ft": mul(swh, M_TO_FT), "period_s": swp, "dir": swd, "time": t_sw},
        "wind_waves": {"height_ft": mul(wwh, M_TO_FT), "period_s": wwp, "dir": wwd},
        "wind": {"speed_kt": mul(ws, MS_TO_KT), "gust_kt": mul(gst, MS_TO_KT),
                 "dir_deg": wdir, "time": t_wind},
        "water_temp_f": None if wtmp is None else round(wtmp * 9 / 5 + 32, 1),
        "water_temp_time": t_wt,
    }


def local_iso(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=PT).isoformat()


def tides():
    common = dict(station=TIDE_STATION, time_zone="lst_ldt", units="english", format="json")
    begin = datetime.now(PT).strftime("%Y%m%d")
    preds = get(COOPS, params=dict(common, product="predictions", datum="MLLW",
                                   interval="hilo", begin_date=begin, range=48)).json()["predictions"]
    out = {"events": [{"time": local_iso(p["t"]), "ft": round(float(p["v"]), 1), "type": p["type"]}
                      for p in preds], "now": None}
    try:  # observed level; not essential
        d = get(COOPS, params=dict(common, product="water_level", datum="MLLW",
                                   date="latest")).json()["data"][0]
        out["now"] = {"time": local_iso(d["t"]), "ft": round(float(d["v"]), 1)}
    except Exception:
        pass
    return out


def harbor_wind():
    common = dict(station=TIDE_STATION, time_zone="lst_ldt", units="english", format="json")
    d = get(COOPS, params=dict(common, product="wind", date="latest")).json()["data"][0]
    # CO-OPS reports knots when units=english
    return {"speed_kt": num(d["s"]), "gust_kt": num(d["g"]), "dir_deg": num(d["d"]),
            "time": local_iso(d["t"])}


def airport_wind():
    p = get(f"https://api.weather.gov/stations/{NWS_STATION}/observations/latest").json()["properties"]
    return {"speed_kt": mul(p["windSpeed"]["value"], KMH_TO_KT),
            "gust_kt": mul(p["windGust"]["value"], KMH_TO_KT),
            "dir_deg": p["windDirection"]["value"], "time": p["timestamp"]}


def forecast(hours=24):
    """Hourly model forecast from Open-Meteo: waves (offshore of the spot) and wind (UCSB)."""
    tz = "America/Los_Angeles"
    errs, marine, wind = [], {}, {}
    try:
        marine = get("https://marine-api.open-meteo.com/v1/marine", params=dict(
            latitude=SPOT_SEA[0], longitude=SPOT_SEA[1], timezone=tz, forecast_days=3,
            length_unit="imperial",
            hourly="wave_height,wave_period,wave_direction,swell_wave_height,"
                   "swell_wave_period,swell_wave_direction")).json()["hourly"]
    except Exception as e:
        errs.append(f"waves: {e}")
    try:
        wind = get("https://api.open-meteo.com/v1/forecast", params=dict(
            latitude=SPOT_LAND[0], longitude=SPOT_LAND[1], timezone=tz, forecast_days=3,
            wind_speed_unit="kn",
            hourly="wind_speed_10m,wind_gusts_10m,wind_direction_10m")).json()["hourly"]
    except Exception as e:
        errs.append(f"wind: {e}")
    if not marine and not wind:
        raise RuntimeError("; ".join(errs))

    def at(block, key, i):
        try:
            return block[key][i]
        except (KeyError, IndexError, TypeError):
            return None

    times = marine.get("time") or wind.get("time")
    widx = {t: i for i, t in enumerate(wind.get("time", []))}
    start = datetime.now(PT).strftime("%Y-%m-%dT%H:00")
    out = []
    for i, t in enumerate(times):
        if t < start:
            continue
        j = widx.get(t)
        out.append({
            "time": local_iso(t.replace("T", " ")),
            "wave_ft": at(marine, "wave_height", i), "wave_period_s": at(marine, "wave_period", i),
            "wave_dir": at(marine, "wave_direction", i),
            "swell_ft": at(marine, "swell_wave_height", i),
            "swell_period_s": at(marine, "swell_wave_period", i),
            "swell_dir": at(marine, "swell_wave_direction", i),
            "wind_kt": at(wind, "wind_speed_10m", j), "gust_kt": at(wind, "wind_gusts_10m", j),
            "wind_dir": at(wind, "wind_direction_10m", j),
        })
        if len(out) == hours:
            break
    return {"source": "Open-Meteo", "hours": out, "warnings": errs}


def main():
    result = {"updated": datetime.now(timezone.utc).isoformat(), "errors": {}}
    for name, fn in (("buoy", buoy), ("tides", tides),
                     ("wind_harbor", harbor_wind), ("wind_airport", airport_wind),
                     ("forecast", forecast)):
        try:
            result[name] = fn()
        except Exception as e:
            result[name] = None
            result["errors"][name] = f"{type(e).__name__}: {e}"
    with open("data.json", "w") as f:
        json.dump(result, f, indent=1)
    if len(result["errors"]) == 5:
        sys.exit(1)


if __name__ == "__main__":
    main()
