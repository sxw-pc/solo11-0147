import json
import unittest
from pathlib import Path
from curriculum.store import connect
from curriculum import degree

ROOT = Path(__file__).resolve().parents[1] / 'fixtures' / 'curriculum'


class DegreeTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')

    def tearDown(self):
        self.db.close()

    def test_program_requirements_and_offers_are_independent(self):
        batch = json.loads((ROOT / 'degree_good.json').read_text(encoding='utf-8'))
        result = degree.ingest(self.db, batch)
        self.assertEqual((result['imported'], result['cases'][0]['missing_count']), (5, 2))
        self.assertEqual(degree.ingest(self.db, batch)['imported'], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM degree_offer').fetchone()[0], 2)
        with self.assertRaises(ValueError):
            degree.ingest(self.db, {'profiles': [dict(batch['profiles'][0], max_credits=0)]})


if __name__ == '__main__':
    unittest.main()
