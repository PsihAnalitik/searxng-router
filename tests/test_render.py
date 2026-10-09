import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('render', ROOT / 'scripts/render-settings.py')
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)


class RenderTests(unittest.TestCase):
    def setUp(self):
        work = ROOT / '.test-work'
        work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup)
        self.output = pathlib.Path(self.temp.name)

    def test_no_credentials_preserves_secret_and_all_packs(self):
        render.render(self.output, {})
        first = (self.output / 'secret').read_text()
        self.assertGreater(len(first.strip()), 32)
        render.render(self.output, {})
        self.assertEqual((self.output / 'secret').read_text(), first)
        for name in ('core', 'backend'):
            text = (self.output / name / 'settings.yml').read_text()
            self.assertIn('secret_key: "' + first.strip(), text)
            self.assertNotIn('name: tavily', text)
            self.assertNotIn('name: mycse', text)
            self.assertNotIn('__SEARXNG_SECRET__', text)
        packs = json.loads((self.output / 'packs.json').read_text())
        self.assertEqual(packs, json.loads((ROOT / 'router/packs.json').read_text()))
        self.assertEqual(len(packs), 11)

    def test_optional_credentials_are_escaped_and_tavily_never_written(self):
        secret = 'quoted"\n__METRICS_PASSWORD__'
        render.render(self.output, {'SEARXNG_SECRET': secret, 'METRICS_PASSWORD': 'a"\nb',
                                   'TAVILY_API_KEY': 'test-provider-secret', 'GOOGLE_CSE_CX': 'personal-id'})
        core = (self.output / 'core/settings.yml').read_text()
        self.assertIn('secret_key: ' + json.dumps(secret), core)
        self.assertIn('open_metrics: ' + json.dumps('a"\nb'), core)
        self.assertIn('CX: "personal-id"', core)
        self.assertIn('engines=google,mycse', core)
        self.assertNotIn('test-provider-secret', core)
        render.render(self.output, {})
        self.assertNotIn('name: mycse', (self.output / 'core/settings.yml').read_text())


if __name__ == '__main__':
    unittest.main()
