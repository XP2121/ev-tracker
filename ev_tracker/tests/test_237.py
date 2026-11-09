"""Regression checks for prominent seasonal metrics."""
from pathlib import Path
import re
import unittest

class SeasonPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src=(Path(__file__).parents[1]/'app/static/index.html').read_text(encoding='utf-8')
        cls.block=cls.src.split('seasonSection.innerHTML=seasonStats.map(a=>',1)[1].split(".join('');",1)[0]

    def test_three_metric_values_share_same_style(self):
        self.assertEqual(self.block.count('class=\"season-value\"'),3)
        self.assertEqual(self.block.count('class=\"season-label\"'),3)
    def test_consumption_uses_dashboard_value(self):
        self.assertIn("f(a.cons,1)",self.block)
        self.assertIn('кВт·год/100 км',self.block)
    def test_both_ranges_use_same_style(self):
        self.assertIn('f(a.full)',self.block)
        self.assertIn('f(a.practical)',self.block)
        self.assertIn('f(rangeCfg.high)',self.block)
        self.assertIn('f(rangeCfg.low)',self.block)
    def test_unchanged_domain_logic(self):
        self.assertIn('EVRange.aggregate',self.src)
        self.assertIn('EVRange.seasonalRows',self.src)
    def test_responsive_presentation(self):
        self.assertRegex(self.src,r'\.season-card \.season-value\{font-size:26px')
        self.assertIn('@media(max-width:560px){.season-card .season-value{font-size:24px}',self.src)

if __name__=='__main__': unittest.main()
