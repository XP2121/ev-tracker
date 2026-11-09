"""Session continuity regressions for EV Tracker v2.3.3."""
from test_ev_tracker import EVTrackerTests


class SessionGapTests(EVTrackerTests):
    def _insert(self, points, entity="sensor.charge_energy"):
        self.m.db.executemany('INSERT OR REPLACE INTO energy_samples VALUES(?,?,?,?)',
                              [(entity, t, 300, v) for t, v in points])
        self.m.db.commit()
        self.m.CACHE.clear()

    def _charge_at(self, start, end, step=300, value=0.15):
        return [(t, value) for t in range(start, end, step)]

    def test_real_case_short_gaps_are_one_session(self):
        # Times 21:55-23:05; gaps 22:25-22:30 and 22:55-23:00.
        h = 3600
        m = 60
        a = 21*h + 55*m
        b = 23*h + 5*m
        skip = {22*h + 25*m, 22*h + 55*m}
        pts = [(t, 0.15) for t in range(a, b, 300) if t not in skip]
        self._insert(pts)
        self.assertEqual(len(pts), 12)
        ss = [x for x in self.m.sessions() if x['t0'] >= a]
        self.assertEqual(len(ss), 1)
        self.assertEqual((ss[0]['t0'], ss[0]['t1']), (a, b))
        self.assertEqual(ss[0]['gap_seconds'], 600)
        self.assertEqual(ss[0]['observed_seconds'], 3600)
        self.assertAlmostEqual(ss[0]['kwh'], 1.8)
        self.assertAlmostEqual(ss[0]['average_power'], 1.8)
        self.assertAlmostEqual(ss[0]['peak'], 1.8)

    def test_explicit_zero_bucket_keeps_short_session_together(self):
        a = 21*3600 + 55*60
        pts = self._charge_at(a, a+30*60) + [(a+30*60, 0.0)] \
              + self._charge_at(a+35*60, a+60*60)
        self._insert(pts)
        ss = [x for x in self.m.sessions() if x['t0'] >= a]
        self.assertEqual(len(ss), 1)
        self.assertEqual(ss[0]['gap_seconds'], 300)
        self.assertAlmostEqual(ss[0]['kwh'], 11*0.15)

    def test_ten_minute_gap_merges_but_fifteen_does_not(self):
        a = 20 * 3600
        pts = [(a, .15),(a+15*60,.15),(a+35*60,.15)]
        self._insert(pts)
        ss = [x for x in self.m.sessions() if x['t0'] >= a]
        self.assertEqual(len(ss), 2)
        older = next(x for x in ss if x['t0']==a)
        self.assertEqual(older['buckets'], 2)
        self.assertEqual(older['gap_seconds'], 600)

    def test_real_30min_unplug_stays_two_sessions(self):
        a = 12*3600
        pts = self._charge_at(a, 14*3600) + self._charge_at(14*3600+30*60,15*3600+40*60)
        self._insert(pts)
        ss = [x for x in self.m.sessions() if x['t0'] >= a]
        self.assertEqual(len(ss), 2)
        self.assertEqual([(x['t0'],x['t1']) for x in ss],
                         [(14*3600+30*60,15*3600+40*60),(a,14*3600)])

    def test_missing_energy_not_created(self):
        a = 20*3600
        pts = [(a, .15),(a+10*60,.15),(a+15*60,.15)]
        self._insert(pts)
        ss = [x for x in self.m.sessions() if x['t0'] >= a]
        self.assertEqual(len(ss),1)
        self.assertAlmostEqual(ss[0]['kwh'], .45)
        self.assertEqual(ss[0]['t1']-ss[0]['t0'],20*60)
        self.assertEqual(ss[0]['observed_seconds'],15*60)

    def test_hourly_history_never_merged_with_5minute(self):
        self._insert([(7200,.15)])
        ss=self.m.sessions()
        self.assertEqual(len(ss),2)
        self.assertEqual(set(x['period'] for x in ss),{3600,300})

if __name__=='__main__':
    import unittest
    unittest.main()
