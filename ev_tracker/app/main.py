import asyncio
import bisect
import datetime as dt
import html
import json
import os
import re
import sqlite3
import time
from collections import defaultdict
from urllib.parse import urlparse

from aiohttp import web, ClientSession, ClientTimeout

TOKEN = os.environ.get("SUPERVISOR_TOKEN")
VER = "2.3.7"
DATA_DIR = os.environ.get("EV_TRACKER_DATA_DIR", "/data")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(DATA_DIR, exist_ok=True)
options_file = os.path.join(DATA_DIR, "options.json")
if os.path.isfile(options_file):
    with open(options_file, encoding="utf-8") as f:
        OPT = json.load(f)
else:
    OPT = {}

# Older installations may have stored the address without the 'index.' subdomain.
def fuel_url(url):
    p = urlparse(url)
    if p.hostname in ("minfin.com.ua", "www.minfin.com.ua") and p.path.rstrip("/") == "/ua/markets/fuel":
        return "https://index.minfin.com.ua/ua/markets/fuel/"
    return url

FURL = fuel_url(OPT.get("fuel_url") or "https://index.minfin.com.ua/ua/markets/fuel/")
RURL = OPT.get("usd_url") or "https://minfin.com.ua/ua/currency/auction/usd/buy/all/"
db = sqlite3.connect(os.path.join(DATA_DIR, "ev.db"), check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS hours2(slot INTEGER, t INTEGER, kwh REAL, PRIMARY KEY(slot, t));
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS cfg(k TEXT PRIMARY KEY, v REAL);
CREATE TABLE IF NOT EXISTS rd(t REAL PRIMARY KEY, km REAL, cons REAL, rate REAL, el REAL, fuel REAL, l100 REAL);
CREATE TABLE IF NOT EXISTS stc(id INTEGER PRIMARY KEY, t REAL, kwh REAL, cost REAL, rate REAL);
CREATE TABLE IF NOT EXISTS xp(id INTEGER PRIMARY KEY, d TEXT, what TEXT, amount REAL, inkm INTEGER, rate REAL);
-- Period is 3600 seconds for old records, 300 seconds for new measurements.
-- Use entity_id as the key to keep history if a sensor is reconfigured.
CREATE TABLE IF NOT EXISTS energy_samples(
  entity TEXT NOT NULL, t INTEGER NOT NULL, period INTEGER NOT NULL, kwh REAL NOT NULL,
  PRIMARY KEY(entity, t, period)
);
CREATE TABLE IF NOT EXISTS sensor_sources(
  entity TEXT PRIMARY KEY, name TEXT NOT NULL, slot INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_energy_samples_time ON energy_samples(t);
-- Persistent status history: events are kept even after HA purges Recorder history.
CREATE TABLE IF NOT EXISTS plug_states (
  entity TEXT NOT NULL, t REAL NOT NULL, connected INTEGER NOT NULL,
  PRIMARY KEY(entity, t)
);
CREATE TABLE IF NOT EXISTS source_plugs (
  entity TEXT PRIMARY KEY, plug_entity TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_plug_states_t ON plug_states(entity,t);
""")


# Older databases stored fuel consumption only in cfg, not in each odometer
# measurement. Preserve existing rows and freeze the best available baseline.
if "l100" not in {r["name"] for r in db.execute("PRAGMA table_info(rd)")}:
    db.execute("ALTER TABLE rd ADD COLUMN l100 REAL")
legacy_l100 = db.execute("SELECT v FROM cfg WHERE k='l100'").fetchone()
db.execute("UPDATE rd SET l100=? WHERE l100 IS NULL",
           (float(legacy_l100[0]) if legacy_l100 else 8.0,))
db.commit()


def cfgv(k, d=0):
    r = db.execute("SELECT v FROM cfg WHERE k=?", (k,)).fetchone()
    return r[0] if r else d


def meta(k):
    r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else ""


def setmeta(k, v):
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))


if not meta("s1_entity") and OPT.get("entity_id"):
    setmeta("s1_entity", OPT["entity_id"])
    db.commit()


def slots():
    return [dict(slot=n, entity=meta(f"s{n}_entity"), name=meta(f"s{n}_name") or f"Реле {n}")
            for n in (1, 2) if meta(f"s{n}_entity")]


def remember_sources():
    for x in slots():
        db.execute("INSERT OR REPLACE INTO sensor_sources(entity,name,slot) VALUES(?,?,?)",
                   (x["entity"], x["name"], x["slot"]))


def remember_plugs():
    """Per energy source binding survives a change of configured measurement slot."""
    for n in (1, 2):
        energy = meta(f"s{n}_entity")
        plug = meta(f"s{n}_plug")
        if energy:
            if plug:
                db.execute("INSERT OR REPLACE INTO source_plugs VALUES(?,?)", (energy, plug))
            else:
                # Keep historic mapping even if UI sensor was deselected? No: an
                # explicit blank disables plug-guided grouping for this source.
                db.execute("DELETE FROM source_plugs WHERE entity=?", (energy,))


def plug_entity_for_energy(entity):
    row = db.execute("SELECT plug_entity FROM source_plugs WHERE entity=?", (entity,)).fetchone()
    return row[0] if row else ""


def selected_plugs():
    return sorted({p for x in slots() if (p := plug_entity_for_energy(x['entity']))})


def record_plug(entity, timestamp, state):
    """Store a real HA binary-sensor observation, never manufacture transitions."""
    if state not in ('on', 'off'):
        return False
    ts = float(timestamp)
    if not 1400000000 <= ts <= time.time() + 300:
        return False
    db.execute("INSERT OR REPLACE INTO plug_states(entity,t,connected) VALUES(?,?,?)",
               (entity, ts, 1 if state == 'on' else 0))
    CACHE.clear()
    return True


def timestamp_of(iso):
    return dt.datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp()


async def sync_plug_history():
    """Recorder backfill for missed state_changed events, with a persistent archive."""
    plug_list = selected_plugs()
    if not plug_list:
        return
    if not TOKEN:
        raise RuntimeError("Не налаштовано доступ до Home Assistant")
    now = int(time.time())
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with ClientSession(headers=headers, timeout=ClientTimeout(total=35)) as c:
        for entity in plug_list:
            # First enablement: recover recent Recorder history. Thereafter,
            # revisit one hour to cover delayed Recorder writes / HA restarts.
            last = int(meta('plug_checked:' + entity) or 0)
            start = max(now - 9*86400, last - 3600) if last else now - 9*86400
            start_iso = dt.datetime.fromtimestamp(start, dt.timezone.utc).isoformat().replace('+00:00','Z')
            end_iso = dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat().replace('+00:00','Z')
            url = 'http://supervisor/core/api/history/period/' + start_iso
            async with c.get(url, params={'filter_entity_id': entity,
                                          'end_time': end_iso,
                                          'no_attributes': '',
                                          'significant_changes_only': '0'}) as r:
                r.raise_for_status()
                history = await r.json()
            for group in history:
                for item in group:
                    state = item.get('state')
                    stamp = item.get('last_changed') or item.get('last_updated')
                    if state in ('on','off') and stamp:
                        record_plug(entity, timestamp_of(stamp), state)
            # Observe current state as a fallback when Recorder history is
            # unavailable or the state has not changed in the backfill window.
            async with c.get('http://supervisor/core/api/states/' + entity) as r:
                r.raise_for_status()
                current = await r.json()
            state = current.get('state')
            stamp = current.get('last_changed') or current.get('last_updated')
            if stamp and state in ('on','off'):
                record_plug(entity, timestamp_of(stamp), state)
            setmeta('plug_checked:' + entity, now)
    db.commit()


async def watch_plug_states():
    """Watch real-time transitions, including short unplug / replug sequences."""
    while True:
        try:
            if not selected_plugs():
                await asyncio.sleep(10)
                continue
            if not TOKEN:
                raise RuntimeError("SUPERVISOR_TOKEN missing")
            async with ClientSession() as c:
                async with c.ws_connect('ws://supervisor/core/websocket', heartbeat=30) as ws:
                    await ws.receive_json()
                    await ws.send_json({'type':'auth','access_token':TOKEN})
                    if (await ws.receive_json()).get('type') != 'auth_ok':
                        raise RuntimeError('Home Assistant WebSocket authorization failed')
                    await ws.send_json({'id':91,'type':'subscribe_events','event_type':'state_changed'})
                    res = await ws.receive_json()
                    if not res.get('success'):
                        raise RuntimeError('Cannot subscribe to HA state_changed')
                    async for msg in ws:
                        if msg.type != 1: # aiohttp WSMsgType.TEXT
                            if msg.type in (8, 257):
                                break
                            continue
                        payload = json.loads(msg.data)
                        event = (payload.get('event') or {}).get('data') or {}
                        entity = event.get('entity_id')
                        if entity not in selected_plugs():
                            continue
                        new = event.get('new_state') or {}
                        state = new.get('state')
                        stamp = new.get('last_changed') or new.get('last_updated')
                        if stamp and record_plug(entity, timestamp_of(stamp), state):
                            db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print('plug watcher reconnect:', exc, flush=True)
        await asyncio.sleep(10)


def migrate_hours():
    """Copy v2.2 hourly data exactly once, preserving the old table as a safety copy."""
    for n in (1, 2):
        source = meta(f"s{n}_loaded") or meta(f"s{n}_entity")
        if not source:
            continue
        if meta(f"s{n}_migrated") == source:
            continue
        db.execute("""INSERT OR IGNORE INTO energy_samples(entity,t,period,kwh)
                      SELECT ?,t,3600,kwh FROM hours2 WHERE slot=?""", (source, n))
        name = meta(f"s{n}_name") or f"Реле {n}"
        db.execute("INSERT OR IGNORE INTO sensor_sources VALUES(?,?,?)", (source, name, n))
        setmeta(f"s{n}_migrated", source)
    remember_sources()
    db.commit()


migrate_hours()
remember_plugs()
db.commit()
CACHE = {}  # Valid until new samples or sensor settings arrive.

# Accept brief telemetry gaps inside an otherwise continuous charge.
# A 30-minute real unplug remains a separate session.
SESSION_GAP_SECONDS = 10 * 60


async def sync():
    """Capture 5-minute HA statistics permanently; backfill old hours too."""
    sl = slots()
    if not sl:
        setmeta("sync_status", "Не вибрано датчиків")
        db.commit()
        return
    remember_sources()
    now = int(time.time())
    # Start at most 9 days back: HA normally deletes 5-minute stats after 10 days.
    recent_start = now - 9 * 86400
    last_h = [db.execute("SELECT MAX(t) FROM energy_samples WHERE entity=? AND period=3600",
                         (x["entity"],)).fetchone()[0] for x in sl]
    last_f = [db.execute("SELECT MAX(t) FROM energy_samples WHERE entity=? AND period=300",
                         (x["entity"],)).fetchone()[0] for x in sl]
    starts = {
        "hour": min((t - 172800 if t else
                     max(1420070400, int(meta("hour_checked:" + x["entity"]) or 1420070400) - 172800))
                    for x, t in zip(sl, last_h)),
        "5minute": min((max(recent_start, t - 7200) if t else recent_start) for t in last_f),
    }
    succeeded = []
    try:
        if not TOKEN:
            raise RuntimeError("Відсутній SUPERVISOR_TOKEN")
        async with ClientSession() as c:
            async with c.ws_connect("ws://supervisor/core/websocket", max_msg_size=0,
                                    ) as ws:
                await ws.receive_json()
                await ws.send_json({"type": "auth", "access_token": TOKEN})
                auth = await ws.receive_json()
                if auth.get("type") != "auth_ok":
                    raise RuntimeError("WebSocket авторизація Home Assistant не вдалася")
                for idx, (period, step) in enumerate((("hour", 3600), ("5minute", 300)), start=1):
                    start = dt.datetime.fromtimestamp(starts[period], dt.timezone.utc).isoformat()
                    await ws.send_json({"id": idx, "type": "recorder/statistics_during_period",
                                        "start_time": start, "statistic_ids": [x["entity"] for x in sl],
                                        "period": period, "types": ["change"]})
                    result = await ws.receive_json()
                    if result.get("id") != idx or not result.get("success"):
                        raise RuntimeError(f"Помилка статистики HA ({period}): {str(result)[:400]}")
                    for x in sl:
                        for y in (result.get("result") or {}).get(x["entity"], []):
                            value = y.get("change")
                            if value is not None and 0 <= float(value) < 1000:
                                db.execute("""INSERT INTO energy_samples(entity,t,period,kwh)
                                    VALUES(?,?,?,?) ON CONFLICT(entity,t,period)
                                    DO UPDATE SET kwh=excluded.kwh""",
                                    (x["entity"], int(y["start"] / 1000), step, float(value)))
                    succeeded.append(period)
                    if period == "hour":
                        for x in sl:
                            setmeta("hour_checked:" + x["entity"], now)
        setmeta("sync_status", "ok")
        setmeta("sync_updated", int(time.time()))
    except Exception as e:
        setmeta("sync_status", f"Помилка: {e}")
        print("sync error:", e, flush=True)
    finally:
        db.commit()
        CACHE.clear()
    # Keep state monitoring independent from energy data availability.
    try:
        await sync_plug_history()
        setmeta('plug_status', 'ok')
    except Exception as e:
        setmeta('plug_status', 'Помилка: ' + str(e))
        print('plug history error:', e, flush=True)
    finally:
        db.commit()


async def connections(req):
    """Known binary connection sensors for selection in the UI."""
    try:
        async with ClientSession(headers={'Authorization': f'Bearer {TOKEN}'}) as c:
            async with c.get('http://supervisor/core/api/states',
                             timeout=ClientTimeout(total=15)) as r:
                r.raise_for_status()
                states = await r.json()
        return web.json_response([{'id': x['entity_id'],
                                   'name': x.get('attributes',{}).get('friendly_name',''),
                                   'state': x.get('state','unknown')}
             for x in states if x.get('entity_id','').startswith('binary_sensor.')
             and 'car_connected' in x['entity_id']])
    except Exception:
        return web.json_response([])


async def entities(req):
    """List HA energy entities for configuration."""
    try:
        async with ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as c:
            async with c.get("http://supervisor/core/api/states", timeout=ClientTimeout(total=15)) as r:
                r.raise_for_status()
                st = await r.json()
        out = [{"id": x["entity_id"], "name": x["attributes"].get("friendly_name", "")} for x in st
               if x["entity_id"].startswith("sensor.") and x["attributes"].get("unit_of_measurement") == "kWh"]
    except Exception:
        out = []
    return web.json_response(out)


def text_of(page):
    t = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", page, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))


def parse_fuel(page):
    """Only ordinary A-95, never premium. First check individual HTML table rows."""
    rows = re.findall(r"<tr\b[^>]*>(.*?)</tr\s*>", page, flags=re.S | re.I)
    rows.append(page)  # Fallback when the site changes its table markup.
    pat = re.compile(r"(?:Бензин\s+)?[АA]\s*[-‑–]?\s*95\b(?!\s*(?:\+|преміум|premium))"
                     r"[^\d]{0,140}(\d{2,3}[.,]\d{1,2})", re.I)
    for row in rows:
        s = text_of(row)
        # In a table row, tolerate multiple columns; prefer ordinary A-95 over other fuels.
        m = pat.search(s)
        if m:
            value = float(m.group(1).replace(",", "."))
            if 30 <= value <= 200:
                return value
    raise ValueError("не знайдено рядок зі звичайним бензином А-95")


def parse_rate(page):
    m = re.search(r"Середній продаж\D{0,30}(\d{2}[.,]\d{1,2})", text_of(page))
    v = float(m.group(1).replace(",", ".")) if m else 0
    if not 20 <= v <= 100:
        raise ValueError("не знайдено «Середній продаж» на сторінці")
    return v


async def grab(url, parse):
    async with ClientSession(headers={"User-Agent": "Mozilla/5.0 (compatible; EVTracker/2.3)"}) as c:
        async with c.get(url, timeout=ClientTimeout(total=25)) as r:
            r.raise_for_status()
            body = await r.text()
            try:
                return parse(body)
            except ValueError as exc:
                raise ValueError(f"{exc}; фактична адреса: {r.url}") from exc


async def fetch_all(force=False):
    if not force and cfgv("auto", 1) != 1:
        return
    for key, url, parse in (("fuel", FURL, parse_fuel), ("rate", RURL, parse_rate)):
        try:
            value = await grab(url, parse)
            db.execute("INSERT OR REPLACE INTO cfg VALUES(?,?)", (key, value))
            st = "ok"
            setmeta(key + "_updated", int(time.time()))
        except Exception as e:
            st = f"Помилка: {e}"
            print("net update error:", key, e, flush=True)
        setmeta(key + "_status", st)
    db.commit()


def effective_samples(entity):
    """Prefer available 5-minute buckets over hourly aggregates.

    Do not replace a *partially filled* 5-minute hour by an entire hour: that
    destroys genuine pauses between sessions. Preserve any difference in a
    separate, explicitly unallocated ledger instead of inventing charging time.
    """
    key = ("__effective__", entity)
    if key in CACHE:
        return CACHE[key]
    hourly = {}
    five = defaultdict(dict)
    for r in db.execute("SELECT t,period,kwh FROM energy_samples WHERE entity=? ORDER BY t", (entity,)):
        t, period, kwh = r["t"], r["period"], r["kwh"]
        if kwh is None or kwh < 0:
            continue
        if period == 3600:
            hourly[t] = (t, period, kwh)
        elif period == 300:
            five[(t // 3600) * 3600][t] = (t, period, kwh)
    samples, unresolved = [], []
    for hour in sorted(set(hourly) | set(five)):
        points = five.get(hour)
        if not points:
            if hour in hourly:
                samples.append(hourly[hour])
            continue
        # Always honor the fine-grained buckets, even when only 11 of 12 exist.
        samples.extend(points.values())
        if hour in hourly:
            observed = sum(x[2] for x in points.values())
            expected = hourly[hour][2]
            difference = expected - observed
            # Small disagreements are normal due to statistics rounding.
            if difference > max(0.015, abs(expected) * 0.01):
                unresolved.append(dict(t=hour, kwh=difference, observed=observed,
                                       hourly=expected, buckets=len(points)))
            elif difference < -max(0.015, abs(expected) * 0.01):
                # HA may have revised statistics. Never add a negative correction
                # to a charging session, but report the discrepancy explicitly.
                unresolved.append(dict(t=hour, kwh=difference, observed=observed,
                                       hourly=expected, buckets=len(points)))
    samples.sort(key=lambda x: x[0])
    CACHE[key] = (samples, unresolved)
    return CACHE[key]


def unresolved_samples(entity):
    return effective_samples(entity)[1]


def state_index(plug):
    """Archived on/off transitions for this connector. Unknown means fallback."""
    rows = list(db.execute('SELECT t,connected FROM plug_states WHERE entity=? ORDER BY t',
                           (plug,)))
    times = [r['t'] for r in rows]
    values = [r['connected'] for r in rows]
    return times, values


def can_join_with_plug(previous_start, previous_end, next_start, period_gap,
                       transitions):
    """An observed unplug ALWAYS splits, however short; known ON joins pauses."""
    if not transitions:
        return period_gap <= SESSION_GAP_SECONDS
    times, values = transitions
    # Check back to the first bucket start, since an unplug may happen inside
    # the last bucket (for example at 14:04 in a 14:00-14:05 bucket).
    i = bisect.bisect_right(times, previous_start)
    j = bisect.bisect_right(times, next_start)
    if any(v == 0 for v in values[i:j]):
        return False
    before = bisect.bisect_right(times, previous_start) - 1
    after = bisect.bisect_right(times, next_start) - 1
    if before >= 0 and after >= 0:
        # Both ends known to be ON, without intervening OFF: same plug-in,
        # even if telemetry disappeared for more than 10 minutes.
        if values[before] == 1 and values[after] == 1:
            return True
        if values[after] == 0:
            return False
    return period_gap <= SESSION_GAP_SECONDS


def sessions():
    """Use Eveus physical connection transitions as session boundaries.

    Energy remains the sum of *measured* buckets. When connection status is not
    configured / unavailable / not recorded yet, old 10-minute gap behavior
    serves as a safe fallback.
    """
    if '__sessions__' in CACHE:
        return CACHE['__sessions__']
    result = []
    for src in db.execute('SELECT * FROM sensor_sources ORDER BY slot,entity'):
        plug = plug_entity_for_energy(src['entity'])
        transitions = state_index(plug) if plug else None
        previous = None
        for t, step, kwh in effective_samples(src['entity'])[0]:
            if kwh < (0.01 if step == 300 else 0.2):
                continue
            power = kwh * 3600 / step
            gap = t - previous['t1'] if previous is not None else None
            connected = (previous is not None and step == previous['period'] and gap >= 0
                         and can_join_with_plug(previous['last_t'], previous['t1'],
                                                t, gap, transitions))
            if connected:
                previous['t1'] = t + step
                previous['last_t'] = t
                previous['kwh'] += kwh
                previous['peak'] = max(previous['peak'], power)
                previous['buckets'] += 1
                previous['observed_seconds'] += step
                previous['gap_seconds'] += gap
            else:
                previous = dict(t0=t, t1=t+step, last_t=t, kwh=kwh, peak=power, period=step,
                                buckets=1, slot=src['slot'], name=src['name'],
                                entity=src['entity'], observed_seconds=step,
                                gap_seconds=0)
                result.append(previous)
    for s in result:
        s['average_power'] = s['kwh'] * 3600 / s['observed_seconds']
        s['resolution'] = '5 хв' if s['period'] == 300 else '1 год'
        s.pop('last_t', None)
    CACHE['__sessions__'] = sorted(result, key=lambda x: -x['t0'])
    return CACHE['__sessions__']


def energy_diagnostics():
    """Expose real data coverage, not only whether the WebSocket call succeeded."""
    end = int(time.time())
    start = end - 86400
    response = []
    for x in slots():
        entity = x["entity"]
        row = db.execute("""SELECT
            SUM(CASE WHEN period=300 AND t>=? THEN 1 ELSE 0 END) AS five_count,
            SUM(CASE WHEN period=3600 AND t>=? THEN 1 ELSE 0 END) AS hour_count,
            MAX(CASE WHEN period=300 THEN t END) AS last_five
            FROM energy_samples WHERE entity=?""", (start, start, entity)).fetchone()
        unresolved = [z for z in unresolved_samples(entity) if z["t"] >= start]
        response.append(dict(entity=entity, name=x["name"], five_count=row["five_count"] or 0,
                             hour_count=row["hour_count"] or 0, last_five=row["last_five"],
                             unresolved_kwh=sum(y["kwh"] for y in unresolved),
                             unresolved_hours=len(unresolved)))
    return response


def measured_energy_points():
    active = {x["entity"] for x in slots()}
    key = ("__measured__", tuple(sorted(active)))
    if key in CACHE:
        return CACHE[key]
    # Coarse residual energy is shown in diagnostics and counted in aggregates,
    # but is NEVER assigned a fabricated five-minute charge session.
    rows = sorted([(t, kwh) for entity in active
                   for t, step, kwh in effective_samples(entity)[0]] +
                  [(u["t"], u["kwh"]) for entity in active
                   for u in unresolved_samples(entity)])
    times = [r[0] for r in rows]
    prefix = [0.0]
    for _, value in rows:
        prefix.append(prefix[-1] + value)
    CACHE[key] = (times, prefix)
    return CACHE[key]


def data():
    loss = cfgv("loss", 10) / 100
    R = list(db.execute("SELECT * FROM rd ORDER BY t"))
    ST = list(db.execute("SELECT * FROM stc ORDER BY t"))
    sensor_times, sensor_totals = measured_energy_points()
    P, cum, now = [], 0, time.time()
    for a, b in zip(R, R[1:]):
        km = b["km"] - a["km"]
        if km <= 0:
            continue
        rate = b["rate"] or 1
        kwh0 = km * b["cons"] / 100
        kwh = kwh0 * (1 + loss)
        s = [x for x in ST if a["t"] <= x["t"] < b["t"]]
        pub = sum(x["kwh"] for x in s)
        home = max(kwh - pub, 0)
        costU = home * b["el"] + sum(x["cost"] for x in s)
        cost = home * b["el"] / rate + sum(x["cost"] / (x["rate"] or 1) for x in s)
        iceU = km * b["l100"] / 100 * b["fuel"]
        cum += iceU / rate - cost
        lo = bisect.bisect_left(sensor_times, a["t"])
        hi = bisect.bisect_left(sensor_times, b["t"])
        P.append(dict(t0=a["t"], t1=b["t"], km=km, kwh0=kwh0, kwh=kwh, home=home, pub=pub,
                      cost=cost, costU=costU, ice=iceU/rate, iceU=iceU, save=iceU/rate-cost,
                      cum=cum, sensor=sensor_totals[hi]-sensor_totals[lo]))
    recent = [p for p in P if p["t1"] >= now - 183 * 86400] or P
    days = sum(p["t1"] - p["t0"] for p in recent) / 86400
    avg = sum(p["save"] for p in recent) / days * 30.44 if days else 0
    car = cfgv("car_usd", 0)
    left = max(car-cum, 0)
    q = lambda sql: [dict(r) for r in db.execute(sql)]
    cfg = {r["k"]: r["v"] for r in db.execute("SELECT * FROM cfg")}
    # New installs and old databases get range defaults without rewriting history.
    for k, value in (("battery_kwh", 28.0), ("range_soc_high", 80.0),
                     ("range_soc_low", 10.0), ("range_min_km", 100.0)):
        cfg.setdefault(k, value)
    stc = q("SELECT * FROM stc ORDER BY t DESC")
    xp = q("SELECT * FROM xp ORDER BY d DESC, id DESC")
    for x in stc: x["usd"] = x["cost"] / (x["rate"] or 1)
    for x in xp: x["usd"] = x["amount"] / (x["rate"] or 1)
    return dict(P=P, cfg=cfg, sum=dict(cum=cum, left=left, avg=avg, months=left/avg if avg > 0 and car else None),
                readings=q("SELECT * FROM rd ORDER BY t DESC"), stc=stc, exp=xp, sessions=sessions(),
                net=dict(fuel=meta("fuel_status"), rate=meta("rate_status"),
                         fuel_at=meta("fuel_updated"), rate_at=meta("rate_updated"),
                         sync=meta("sync_status"), sync_at=meta("sync_updated")),
                diagnostics=energy_diagnostics(),
                sources=q("SELECT * FROM sensor_sources ORDER BY slot,entity"),
                slots=slots(), txt={k: meta(k) for k in ("s1_entity", "s1_name", "s2_entity", "s2_name", "s1_plug", "s2_plug")}, ver=VER)


async def post(req):
    k = req.match_info["k"]
    b = await req.json()
    nz = lambda v, d: d if v in (None, "") else float(v)
    try:
        if k == "odo":
            if b.get("orig") is not None and b["orig"] != b["t"]:
                db.execute("DELETE FROM rd WHERE t=?", (b["orig"],))
            gasoline_consumption = nz(b.get("l100"), cfgv("l100", 8))
            if gasoline_consumption <= 0:
                raise ValueError("Витрата бензину повинна бути більшою за нуль")
            db.execute("""INSERT OR REPLACE INTO rd
                           (t,km,cons,rate,el,fuel,l100) VALUES(?,?,?,?,?,?,?)""",
                       (b["t"], b["km"], nz(b.get("cons"), cfgv("cons")),
                        nz(b.get("rate"), cfgv("rate", 1)), nz(b.get("el"), cfgv("el")),
                        nz(b.get("fuel"), cfgv("fuel")), gasoline_consumption))
        elif k == "delodo": db.execute("DELETE FROM rd WHERE t=?", (b["t"],))
        elif k == "stc": db.execute("INSERT INTO stc(t,kwh,cost,rate) VALUES(?,?,?,?)",
                                     (b["t"], b["kwh"], b["cost"], nz(b.get("rate"), cfgv("rate", 1))))
        elif k == "delstc": db.execute("DELETE FROM stc WHERE id=?", (b["id"],))
        elif k == "exp": db.execute("INSERT INTO xp(d,what,amount,inkm,rate) VALUES(?,?,?,?,?)",
                                    (b["d"], b["what"].strip(), b["amount"],
                                     1 if b.get("inkm") else 0, nz(b.get("rate"), cfgv("rate", 1))))
        elif k == "delexp": db.execute("DELETE FROM xp WHERE id=?", (b["id"],))
        elif k == "cfg":
            # Validate range inputs before touching the database. Changing these
            # settings affects only the displayed projections, not historic data.
            battery = nz(b.get("battery_kwh"), cfgv("battery_kwh", 28))
            high = nz(b.get("range_soc_high"), cfgv("range_soc_high", 80))
            low = nz(b.get("range_soc_low"), cfgv("range_soc_low", 10))
            minimum = nz(b.get("range_min_km"), cfgv("range_min_km", 100))
            if not 0 < battery <= 150:
                raise ValueError("Доступна ємність батареї повинна бути більшою за 0 і не більшою за 150 кВт·год")
            if not 0 <= low < high <= 100:
                raise ValueError("Початковий SOC повинен бути більшим за мінімальний, у межах 0–100%")
            if not 0 <= minimum <= 10000:
                raise ValueError("Мінімальний пробіг має бути в межах 0–10000 км")
            for a in ("cons", "loss", "l100", "el", "fuel", "rate", "car_usd", "auto", "remind",
                      "battery_kwh", "range_soc_high", "range_soc_low", "range_min_km"):
                if b.get(a) not in (None, ""):
                    db.execute("INSERT OR REPLACE INTO cfg VALUES(?,?)", (a, float(b[a])))
            for a in ("s1_entity", "s1_name", "s2_entity", "s2_name", "s1_plug", "s2_plug"):
                if a in b: setmeta(a, (b[a] or "").strip())
            remember_sources()
            remember_plugs()
            CACHE.clear()
        elif k == "import":
            if not isinstance(b.get("rd"), list):
                return web.json_response({"error": "Некоректний формат резервної копії"}, status=400)
            # Transaction: corrupted input must never partly overwrite the user's data.
            backup_l100 = float((b.get("cfg") or {}).get("l100", cfgv("l100", 8)))
            with db:
                for table in ("rd", "stc", "xp", "cfg"):
                    db.execute(f"DELETE FROM {table}")
                # Backups from <=2.3.1 have no per-reading l100. For those,
                # use their saved config value, never an unrelated new default.
                for x in b["rd"]:
                    db.execute("""INSERT OR REPLACE INTO rd
                                   (t,km,cons,rate,el,fuel,l100) VALUES(?,?,?,?,?,?,?)""",
                               (x["t"], x["km"], x["cons"], x["rate"], x["el"],
                                x["fuel"], x.get("l100", backup_l100)))
                for x in b.get("stc", []):
                    db.execute("INSERT OR REPLACE INTO stc VALUES(?,?,?,?,?)",
                               (x["id"], x["t"], x["kwh"], x["cost"], x["rate"]))
                for x in b.get("xp", []):
                    db.execute("INSERT OR REPLACE INTO xp VALUES(?,?,?,?,?,?)",
                               (x["id"], x["d"], x["what"], x["amount"], x["inkm"], x["rate"]))
                for a, v in (b.get("cfg") or {}).items():
                    db.execute("INSERT OR REPLACE INTO cfg VALUES(?,?)", (a, float(v)))
                for a in ("s1_entity", "s1_name", "s2_entity", "s2_name", "s1_plug", "s2_plug"):
                    if a in (b.get("meta") or {}): setmeta(a, b["meta"][a])
                if "energy_samples" in b:
                    db.execute("DELETE FROM energy_samples")
                    for x in b["energy_samples"]:
                        db.execute("INSERT INTO energy_samples VALUES(?,?,?,?)",
                                   (x["entity"], x["t"], x["period"], x["kwh"]))
                if "sources" in b:
                    db.execute("DELETE FROM sensor_sources")
                    for x in b["sources"]:
                        db.execute("INSERT INTO sensor_sources VALUES(?,?,?)",
                                   (x["entity"], x["name"], x["slot"]))
                if 'plug_states' in b:
                    db.execute('DELETE FROM plug_states')
                    for x in b['plug_states']:
                        db.execute('INSERT INTO plug_states VALUES(?,?,?)',
                                   (x['entity'], x['t'], x['connected']))
                if 'source_plugs' in b:
                    db.execute('DELETE FROM source_plugs')
                    for x in b['source_plugs']:
                        db.execute('INSERT INTO source_plugs VALUES(?,?)',
                                   (x['entity'], x['plug_entity']))
                remember_sources()
                # Older backups have no plug selections. Retain current
                # settings unless importing a newer backup with explicit keys.
                if any(k in (b.get('meta') or {}) for k in ('s1_plug','s2_plug')):
                    remember_plugs()
            CACHE.clear()
        elif k == "sync": await sync()
        elif k == "net": await fetch_all(True)
        else:
            return web.json_response({"error": "Невідома дія"}, status=404)
        db.commit()
    except (ValueError, TypeError, KeyError, sqlite3.Error) as e:
        db.rollback()
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response(data())


async def export(req):
    q = lambda sql: [dict(r) for r in db.execute(sql)]
    return web.json_response(dict(app="ev_tracker", ver=VER, rd=q("SELECT * FROM rd ORDER BY t"),
        stc=q("SELECT * FROM stc"), xp=q("SELECT * FROM xp"),
        cfg={r["k"]: r["v"] for r in db.execute("SELECT * FROM cfg")},
        meta={k: meta(k) for k in ("s1_entity", "s1_name", "s2_entity", "s2_name", "s1_plug", "s2_plug")},
        energy_samples=q("SELECT * FROM energy_samples ORDER BY entity,t,period"),
        sources=q("SELECT * FROM sensor_sources ORDER BY entity"),
        plug_states=q("SELECT * FROM plug_states ORDER BY entity,t"),
        source_plugs=q("SELECT * FROM source_plugs ORDER BY entity")))


async def loop(app):
    async def run():
        last_net = 0
        while True:
            await sync()
            if time.time() - last_net >= 6 * 3600:
                await fetch_all()
                last_net = time.time()
            await asyncio.sleep(300)
    app["t"] = asyncio.create_task(run())
    app["plug_watch"] = asyncio.create_task(watch_plug_states())


async def stop(app):
    for key in ('t', 'plug_watch'):
        app[key].cancel()
    for key in ('t', 'plug_watch'):
        try:
            await app[key]
        except asyncio.CancelledError:
            pass
    db.commit()


@web.middleware
async def nocache(req, handler):
    resp = await handler(req)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


app = web.Application(middlewares=[nocache], client_max_size=200 * 1024 ** 2)
app.on_startup.append(loop)
app.on_cleanup.append(stop)
app.add_routes([
    web.get("/", lambda r: web.FileResponse(os.path.join(APP_DIR, "static/index.html"))),
    web.get("/api/data", lambda r: web.json_response(data())),
    web.get("/api/entities", entities),
    web.get("/api/connections", connections),
    web.get("/api/export", export),
    web.post("/api/{k}", post),
    web.static("/static", os.path.join(APP_DIR, "static")),
])

if __name__ == "__main__":
    web.run_app(app, port=8099)
