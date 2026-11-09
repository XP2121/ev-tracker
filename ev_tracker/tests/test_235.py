"""V2.3.5: user-configurable range without modifying existing financial or charging data."""
import asyncio
import json
import unittest
from test_ev_tracker import EVTrackerTests

class Request:
    def __init__(self, route, payload):
        self.match_info = {'k': route}
        self.payload = payload
    async def json(self):
        return self.payload

class RangeBackendTests(EVTrackerTests):
    def _post(self, route, payload):
        return asyncio.run(self.m.post(Request(route, payload)))

    def test_defaults_on_old_db(self):
        cfg = self.m.data()['cfg']
        for key, expected in [('battery_kwh',28),('range_soc_high',80),('range_soc_low',10),('range_min_km',100)]:
            self.assertEqual(cfg[key], expected)

    def test_settings_saved_without_changing_history_and_exported(self):
        self._post('odo', dict(t=1000, km=1000, cons=15, rate=40, el=4, fuel=65, l100=8))
        self._post('odo', dict(t=2000, km=1100, cons=17, rate=40, el=4, fuel=65, l100=8))
        original = self.m.data()['P']
        result = self._post('cfg', dict(battery_kwh=26.5, range_soc_high=90, range_soc_low=15, range_min_km=75))
        self.assertEqual(result.status,200)
        cfg = self.m.data()['cfg']
        self.assertEqual((cfg['battery_kwh'],cfg['range_soc_high'],cfg['range_soc_low'],cfg['range_min_km']), (26.5,90,15,75))
        self.assertEqual(original, self.m.data()['P'])
        exported = json.loads(asyncio.run(self.m.export(None)).text)
        self.assertEqual(exported['cfg']['battery_kwh'],26.5)

    def test_invalid_soc_rejected_as_whole_update(self):
        r = self._post('cfg',dict(battery_kwh=26,range_soc_high=20,range_soc_low=80,cons=22))
        self.assertEqual(r.status,400)
        self.assertNotIn('cons', self.m.data()['cfg'])
        self.assertEqual(self.m.data()['cfg']['battery_kwh'],28)

    def test_range_config_roundtrip_in_backup(self):
        self._post('cfg', dict(battery_kwh=27.3,range_soc_high=95,range_soc_low=5,range_min_km=200))
        backed_up=json.loads(asyncio.run(self.m.export(None)).text)
        self._post('cfg', dict(battery_kwh=28,range_soc_high=80,range_soc_low=10,range_min_km=100))
        res=self._post('import',backed_up)
        self.assertEqual(res.status,200)
        self.assertEqual(self.m.data()['cfg']['battery_kwh'],27.3)
        self.assertEqual(self.m.data()['cfg']['range_min_km'],200)

if __name__ == '__main__':
    unittest.main()
