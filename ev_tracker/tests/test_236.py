"""Regression checks: moving range/seasons UI must not move payback data."""
from html.parser import HTMLParser
from pathlib import Path
import unittest


class SectionsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.section = None
        self.components = {}
        self.nav = {}
        self.button = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'section':
            self.section = a.get('id')
        if a.get('id'):
            self.components[a['id']] = self.section
        if tag == 'button' and a.get('data-t'):
            self.button = a['data-t']
            self.nav[self.button] = ''

    def handle_data(self, data):
        if self.button is not None:
            self.nav[self.button] += data

    def handle_endtag(self, tag):
        if tag == 'section':
            self.section = None
        if tag == 'button':
            self.button = None


class SeasonNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        p = Path(__file__).parents[1]/'app'/'static'/'index.html'
        cls.source = p.read_text(encoding='utf-8')
        cls.parsed = SectionsParser()
        cls.parsed.feed(cls.source)

    def test_tab_in_sidebar_and_section_exists(self):
        self.assertIn('Сезони', self.parsed.nav.get('s',''))
        self.assertEqual(self.parsed.components.get('t-s'),'t-s')
        self.assertIn("#side button,section",self.source)

    def test_all_season_widgets_in_season_tab(self):
        for element in ['ysel','stiles','rangechart','range-months']:
            self.assertEqual(self.parsed.components.get(element),'t-s',element)
        self.assertIn('<div class=cc data-c=c4>',self.source)
        self.assertIn('<section id=t-s>',self.source)

    def test_payback_widgets_not_moved(self):
        for element in ['avgb','ptiles','ntiles','etiles','explist']:
            self.assertEqual(self.parsed.components.get(element),'t-p',element)

    def test_only_one_seasons_section_and_no_old_season_header(self):
        self.assertEqual(self.source.count('<section id=t-s>'),1)
        self.assertNotIn('<h2 class=s>Сезони</h2>',self.source)


if __name__=='__main__':
    unittest.main()
