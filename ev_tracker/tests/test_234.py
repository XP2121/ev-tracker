"""Eveus connection sensor integration and persistent history regressions."""
from test_ev_tracker import EVTrackerTests
from test_232 import Request
import asyncio
import json
import time


class EveusPlugTests(EVTrackerTests):
    def setup_stream(self, active=True):
        m = self.m
        plug = 'binary_sensor.eveus_ev_charger_car_connected'
        if active:
            m.setmeta('s1_plug', plug)
        m.remember_plugs()
        m.db.commit()
        m.CACHE.clear()
        return plug

    def samples(self, start, intervals, gaps=()):
        rows=[('sensor.charge_energy',start+i*300,300,.16)
              for i in range(intervals) if i not in gaps]
        self.m.db.executemany('INSERT OR REPLACE INTO energy_samples VALUES(?,?,?,?)',rows)
        self.m.db.commit()
        self.m.CACHE.clear()

    def test_continuous_connected_charge_unites_25_minute_telemetry_gap(self):
        m = self.m; plug = self.setup_stream()
        start=int(time.time())//300*300-18000
        self.samples(start,14,gaps=(4,5,6,7,8))  # a 25min gap
        m.record_plug(plug,start-2*60,'on')
        ss=[x for x in m.sessions() if x['t0']>=start]
        self.assertEqual(len(ss),1)
        self.assertEqual(ss[0]['t1']-ss[0]['t0'],14*300)
        self.assertAlmostEqual(ss[0]['kwh'], 9*.16)
        self.assertEqual(ss[0]['gap_seconds'],25*60)

    def test_real_5_minute_unplug_splits_even_when_gap_is_short(self):
        m=self.m; plug=self.setup_stream()
        start=int(time.time())//300*300-18000
        self.samples(start,14,gaps=(6,))
        m.record_plug(plug,start-60,'on')
        m.record_plug(plug,start+6*300+5,'off')
        m.record_plug(plug,start+7*300-3,'on')
        ss=[x for x in m.sessions() if x['t0']>=start]
        self.assertEqual(len(ss),2)
        self.assertEqual([(s['t0'],s['t1']) for s in ss],
                         [(start+7*300,start+14*300),(start,start+6*300)])
        self.assertAlmostEqual(sum(s['kwh'] for s in ss),13*.16)

    def test_real_30_minute_unplug_splits(self):
        m=self.m; plug=self.setup_stream()
        start=int(time.time())//300*300-18000
        self.samples(start,25,gaps=range(12,18))
        m.record_plug(plug,start-60,'on')
        m.record_plug(plug,start+12*300+30,'off')
        m.record_plug(plug,start+18*300-20,'on')
        ss=[x for x in m.sessions() if x['t0']>=start]
        self.assertEqual(len(ss),2)

    def test_unconfigured_sensor_falls_back_to_ten_minute_gap(self):
        m=self.m; self.setup_stream(False)
        start=int(time.time())//300*300-18000
        self.samples(start,14,gaps=(4,5,6,7,8))
        ss=[x for x in m.sessions() if x['t0']>=start]
        self.assertEqual(len(ss),2)

    def test_explicit_unknown_states_are_ignored(self):
        m=self.m; plug=self.setup_stream()
        self.assertFalse(m.record_plug(plug,time.time(),'unavailable'))
        self.assertEqual(m.db.execute('SELECT COUNT(*) FROM plug_states').fetchone()[0],0)

    def test_archive_exports_mapping_and_statuses(self):
        m=self.m; plug=self.setup_stream()
        now=int(time.time())-1000
        m.record_plug(plug,now,'on');m.record_plug(plug,now+20,'off')
        out=json.loads(asyncio.run(m.export(None)).text)
        self.assertEqual(len(out['plug_states']),2)
        self.assertEqual(out['source_plugs'][0]['plug_entity'],plug)
        response=asyncio.run(m.post(Request('import',out)))
        self.assertEqual(response.status,200,response.text)
        self.assertEqual(m.db.execute('SELECT COUNT(*) FROM plug_states').fetchone()[0],2)
        self.assertEqual(m.plug_entity_for_energy('sensor.charge_energy'),plug)

    def test_status_migration_keeps_old_hourly_data(self):
        m=self.m; plug=self.setup_stream()
        m.record_plug(plug,time.time()-10,'on')
        self.assertEqual(m.db.execute('SELECT COUNT(*) FROM hours2').fetchone()[0],1)
        self.assertEqual(m.db.execute('SELECT COUNT(*) FROM energy_samples').fetchone()[0],1)


if __name__=='__main__':
    import unittest
    unittest.main()
