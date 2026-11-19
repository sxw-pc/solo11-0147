import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from curriculum import handout, navigation
from curriculum.store import connect


class HandoutCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'test.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_label_coverage_and_replay(self):
        data = json.loads((ROOT / 'fixtures' / 'curriculum' / 'handout_good.json').read_text(encoding='utf-8'))
        self.assertEqual(handout.ingest(self.db, data)['imported'], 1)
        self.assertEqual(navigation.check(self.db, 'C3-notes')['status'], 'ready')
        self.assertEqual(self.db.execute("SELECT return_to FROM handout_node WHERE node_id='note'").fetchone()[0], 'intro')
        self.assertEqual(handout.ingest(self.db, data)['imported'], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM handout_node').fetchone()[0], 4)

    def test_missing_label(self):
        data = json.loads((ROOT / 'fixtures' / 'curriculum' / 'handout_good.json').read_text(encoding='utf-8'))
        data[0]['nodes'][2]['label'] = ''
        data[0]['event_id'] = 'handout-empty'
        handout.ingest(self.db, data)
        self.assertEqual(navigation.check(self.db, 'C3-notes')['missing_labels'], ['note'])
