"""Run with python -m unittest discover -s tests -v (aiohttp required)."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1]


class EVTrackerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ['EV_TRACKER_DATA_DIR'] = self.dir.name
        (Path(self.dir.name) / 'options.json').write_text(json.dumps({
            'fuel_url': 'https://minfin.com.ua/ua/markets/fuel/'
        }))
        # Seed v2.2 history to exercise the automatic migration.
        with sqlite3.connect(str(Path(self.dir.name) / 'ev.db')) as db:
            db.execute('CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT)')
            db.execute('CREATE TABLE hours2(slot INTEGER,t INTEGER,kwh REAL,PRIMARY KEY(slot,t))')
            db.executemany('INSERT INTO meta VALUES(?,?)', [
                ('s1_entity','sensor.charge_energy'), ('s1_name','Гараж'),
                ('s1_loaded','sensor.charge_energy')])
            db.execute('INSERT INTO hours2 VALUES(1,3600,3.1)')
        spec = importlib.util.spec_from_file_location('ev_module', str(HERE / 'app/main.py'))
        self.m = importlib.util.module_from_spec(spec)
        sys.modules['ev_module'] = self.m
        spec.loader.exec_module(self.m)

    def tearDown(self):
        self.m.db.close()
        self.dir.cleanup()

    def test_url_correction(self):
        self.assertEqual(self.m.FURL, 'https://index.minfin.com.ua/ua/markets/fuel/')

    def test_fuel_selects_ordinary_not_premium(self):
        page = '''<table><tr><td>Бензин А-95 преміум</td><td>93,89</td></tr>
            <tr><td><a>Бензин А-95</a></td><td>90,07</td><td>-0.02</td></tr>
            <tr><td>Бензин А-92</td><td>86,47</td></tr></table>'''
        self.assertAlmostEqual(self.m.parse_fuel(page), 90.07)
        self.assertAlmostEqual(self.m.parse_fuel('Бензин А-95 преміум 93,89 Бензин А-95 90,07'),90.07)

    def test_old_hours_migrated(self):
        row = self.m.db.execute('SELECT * FROM energy_samples WHERE entity=?', ('sensor.charge_energy',)).fetchone()
        self.assertEqual((row['t'],row['period'],row['kwh']), (3600,3600,3.1))

    def test_five_minute_and_hourly_no_double_count(self):
        # The complete fine-grained hour replaces its hourly backup.
        e='sensor.charge_energy'
        self.m.db.execute('INSERT INTO energy_samples VALUES(?,?,?,?)',(e,7200,3600,6.0))
        self.m.db.executemany('INSERT INTO energy_samples VALUES(?,?,?,?)',[
            (e,7200+i*300,300,0.5) for i in range(12)])
        self.m.db.commit()
        self.m.CACHE.clear()
        p=self.m.effective_samples(e)[0]
        self.assertEqual(len(p),13)
        self.assertAlmostEqual(sum(v for _,_,v in p),9.1)
        s=self.m.sessions()
        self.assertEqual(len(s),2)  # Different resolutions remain clearly separated.
        self.assertAlmostEqual(sum(x['kwh'] for x in s),9.1)
        self.assertEqual({x['resolution'] for x in s},{'5 хв','1 год'})

    def test_incomplete_historical_hour_preserves_five_minute(self):
        e='sensor.charge_energy'
        self.m.db.execute('INSERT INTO energy_samples VALUES(?,?,?,?)',(e,7200,3600,6.0))
        self.m.db.executemany('INSERT INTO energy_samples VALUES(?,?,?,?)',[
            (e,7200+i*300,300,0.5) for i in range(11)])
        self.m.CACHE.clear()
        fine, unresolved = self.m.effective_samples(e)
        self.assertAlmostEqual(sum(v for _,_,v in fine),8.6)
        self.assertEqual([s[1] for s in fine],[3600]+[300]*11)
        self.assertAlmostEqual(unresolved[0]['kwh'],0.5)

    def test_missing_zero_five_minute_bucket_does_not_merge_sessions(self):
        """Regression: 12:00–14:00 and 14:30–15:40 are distinct.
        One missing 5-minute row (a zero during the break) must not
        downgrade the entire 14:00 hour to hourly precision.
        """
        e = 'sensor.charge_energy'
        base = 12 * 3600
        rows = []
        # Hours 12 and 13: continuously charging, all 5-minute readings.
        for h in (12, 13):
            t = h * 3600
            rows.append((e,t,3600,1.8))
            rows += [(e,t+300*i,300,0.15) for i in range(12)]
        # 14:00–14:30 idle; intentionally omit one zero-valued statistic.
        # 14:30–15:00 charging.
        rows.append((e,14*3600,3600,0.9))
        rows += [(e,14*3600+300*i,300,0.0 if i<6 else 0.15)
                 for i in range(12) if i!=3]
        # 15:00–15:40 charging, remaining buckets idle.
        rows.append((e,15*3600,3600,1.2))
        rows += [(e,15*3600+300*i,300,0.15 if i<8 else 0.0)
                 for i in range(12)]
        self.m.db.executemany('INSERT OR REPLACE INTO energy_samples VALUES(?,?,?,?)',rows)
        self.m.CACHE.clear()
        today = [x for x in self.m.sessions() if x['t0'] >= base]
        self.assertEqual(len(today), 2)
        self.assertEqual([(x['t0'],x['t1']) for x in today],
                         [(14*3600+30*60,15*3600+40*60),(12*3600,14*3600)])
        self.assertTrue(all(x['resolution']=='5 хв' for x in today))
        self.assertAlmostEqual(sum(x['kwh'] for x in today), 5.7)

    def test_missing_nonzero_five_minute_energy_is_not_fabricated_as_session(self):
        e = 'sensor.charge_energy'
        t = 22 * 3600
        self.m.db.execute('INSERT INTO energy_samples VALUES(?,?,?,?)',(e,t,3600,1.0))
        self.m.db.executemany('INSERT INTO energy_samples VALUES(?,?,?,?)',
                              [(e,t+i*300,300,0.1) for i in range(8)])
        self.m.CACHE.clear()
        samples,unresolved=self.m.effective_samples(e)
        self.assertEqual(len([v for v in samples if t<=v[0]<t+3600]),8)
        self.assertAlmostEqual(unresolved[0]['kwh'],0.2)
        times, totals = self.m.measured_energy_points()
        self.assertAlmostEqual(totals[-1],4.1)

    def test_no_1000_sessions_cap(self):
        e='sensor.charge_energy'
        self.m.db.executemany('INSERT INTO energy_samples VALUES(?,?,?,?)',[
            (e,100000 + 7200*i,3600,1.2) for i in range(1100)])
        self.m.CACHE.clear()
        self.assertEqual(len(self.m.sessions()),1101)

    def test_previous_sensor_history_remains_when_replaced(self):
        self.m.setmeta('s1_entity','sensor.other_energy')
        self.m.remember_sources()
        self.m.db.commit()
        self.assertIn('sensor.charge_energy', {s['entity'] for s in self.m.sessions()})
        self.assertEqual(self.m.db.execute('SELECT COUNT(*) FROM energy_samples').fetchone()[0],1)


    def test_export_includes_entire_sample_history(self):
        e='sensor.charge_energy'
        self.m.db.executemany('INSERT INTO energy_samples VALUES(?,?,?,?)',[
            (e,100000 + 7200*i,3600,1.2) for i in range(1100)])
        response = asyncio.run(self.m.export(None))
        obj = json.loads(response.text)
        self.assertEqual(len(obj['energy_samples']),1101)
        self.assertEqual(obj['sources'][0]['entity'],e)

    def test_websocket_sync_requests_both_periods(self):
        mod = self.m
        sent=[]
        entity='sensor.charge_energy'
        class WS:
            n=0
            async def __aenter__(self): return self
            async def __aexit__(self,*args): return None
            async def send_json(self,payload): sent.append(payload)
            async def receive_json(self):
                self.n+=1
                if self.n==1: return {'type':'auth_required'}
                if self.n==2: return {'type':'auth_ok'}
                period=sent[-1]['period']
                if period=='hour':
                    return {'id':1,'success':True,'result':{entity:[{'start':7200*1000,'change':6.0}]}}
                return {'id':2,'success':True,'result':{entity:[
                    {'start':(7200+300*i)*1000,'change':0.5} for i in range(12)]}}
        class Session:
            def __init__(self,*args,**kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self,*args): return None
            def ws_connect(self,*args,**kwargs): return WS()
        old_session,old_token=mod.ClientSession,mod.TOKEN
        try:
            mod.ClientSession=Session
            mod.TOKEN='test-token'
            asyncio.run(mod.sync())
        finally:
            mod.ClientSession,mod.TOKEN=old_session,old_token
        self.assertEqual([i['period'] for i in sent if i.get('period')],['hour','5minute'])
        self.assertEqual(mod.db.execute('SELECT COUNT(*) FROM energy_samples').fetchone()[0],14)
        self.assertEqual(mod.meta('sync_status'),'ok')
        self.assertAlmostEqual(sum(v for _,_,v in mod.effective_samples(entity)[0]),9.1)


if __name__ == '__main__':
    unittest.main()
