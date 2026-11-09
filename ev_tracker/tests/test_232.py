"""v2.3.2 data regressions: individual fuel baselines and legacy imports."""
from test_ev_tracker import EVTrackerTests
import asyncio
import json

class Request:
    def __init__(self, route, payload):
        self.match_info={'k':route}
        self.payload=payload
    async def json(self): return self.payload

class NewBehaviorTests(EVTrackerTests):
    def _call(self, route, payload):
        response=asyncio.run(self.m.post(Request(route,payload)))
        self.assertEqual(response.status,200,response.text)
        return json.loads(response.text)

    def test_consumption_snapshots_remain_stable_when_settings_change(self):
        self._call('cfg',{'l100':8, 'loss':0, 'el':4, 'car_usd':12000})
        r={'km':100,'cons':15,'rate':40,'el':4,'fuel':60,'l100':8,'t':10000}
        self._call('odo',r)
        self._call('cfg',{'l100':6})
        self._call('odo',{**r, 't':20000,'km':200,'fuel':65,'l100':6})
        self._call('cfg',{'l100':4})
        out=self.m.data()
        self.assertEqual([x['l100'] for x in out['readings']],[6,8])
        self.assertAlmostEqual(out['P'][0]['iceU'],390)
        self.assertAlmostEqual(out['P'][0]['costU'],60)
        self.assertAlmostEqual(out['P'][0]['save'],(390-60)/40)

    def test_expense_impact_is_only_on_investment(self):
        self._call('cfg',{'l100':8,'loss':0,'el':4,'car_usd':10000})
        r={'km':100,'cons':15,'rate':40,'el':4,'fuel':65,'l100':8,'t':10000}
        self._call('odo',r)
        self._call('odo',{**r,'t':20000,'km':200})
        before=self.m.data()['sum']['cum']
        self._call('exp',{'d':'2026-11-01','what':'Зарядка','amount':24000,'inkm':1,'rate':40})
        self.assertAlmostEqual(self.m.data()['sum']['cum'],before)
        self.assertEqual(self.m.data()['exp'][0]['usd'],600)

    def test_import_old_backup_uses_backup_consumption(self):
        backup={'rd':[{'t':10,'km':100,'cons':12,'rate':42,'el':4,'fuel':60},
                      {'t':20,'km':200,'cons':12,'rate':42,'el':4,'fuel':65}],
                'cfg':{'l100':7.5,'car_usd':13000}, 'stc':[],'xp':[]}
        self._call('import',backup)
        self.assertEqual([x['l100'] for x in self.m.data()['readings']],[7.5,7.5])
        ex=json.loads(asyncio.run(self.m.export(None)).text)
        self.assertEqual(ex['rd'][0]['l100'],7.5)
        ex['rd'][0]['l100']=8.5
        self._call('import',ex)
        self.assertEqual(self.m.data()['readings'][1]['l100'],8.5)

    def test_unchecked_expense_not_added(self):
        self._call('exp',{'d':'2026-11-01','what':'Something','amount':700,'inkm':0,'rate':40})
        self.assertEqual(self.m.data()['exp'][0]['inkm'],0)


class LegacySchemaMigrationTests(__import__('unittest').TestCase):
    def test_old_rd_without_l100_gets_snapshot(self):
        import os, tempfile, sqlite3, importlib.util, sys
        from pathlib import Path
        with tempfile.TemporaryDirectory() as dirname:
            os.environ['EV_TRACKER_DATA_DIR']=dirname
            path=Path(dirname)/'ev.db'
            with sqlite3.connect(path) as d:
                d.execute('CREATE TABLE rd(t REAL PRIMARY KEY,km REAL,cons REAL,rate REAL,el REAL,fuel REAL)')
                d.execute('CREATE TABLE cfg(k TEXT PRIMARY KEY,v REAL)')
                d.execute("INSERT INTO cfg VALUES('l100',7.2)")
                d.execute('INSERT INTO rd VALUES(1000,10000,16,42,4.32,65)')
            name='legacy_ev_232'
            spec=importlib.util.spec_from_file_location(name,str(__import__('pathlib').Path(__file__).resolve().parents[1]/'app/main.py'))
            m=importlib.util.module_from_spec(spec)
            sys.modules[name]=m
            spec.loader.exec_module(m)
            try:
                fields=[r['name'] for r in m.db.execute('PRAGMA table_info(rd)')]
                self.assertIn('l100',fields)
                self.assertAlmostEqual(m.db.execute('SELECT l100 FROM rd').fetchone()[0],7.2)
            finally:
                m.db.close()
