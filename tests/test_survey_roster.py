import json
import unittest
from pathlib import Path

from curriculum.store import connect
from curriculum import catalog, survey, survey_roster

ROOT = Path(__file__).resolve().parents[1] / 'fixtures' / 'curriculum'


class SurveyRosterTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')
        catalog.ingest(self.db, json.loads((ROOT / 'catalog_good.json').read_text(encoding='utf-8')))
        survey.ingest(self.db, {'forms': [{
            'event_id': 'f-1', 'course_id': 'C3', 'year': 2026,
            'form_id': 'teaching', 'version': 1, 'question': 'teaching quality',
            'issuer': 'school', 'event_time': '2026-03-01T00:00:00Z',
        }]})

    def tearDown(self):
        self.db.close()

    def test_invitation_ledger_and_response_rate(self):
        batch = json.loads((ROOT / 'survey_roster_good.json').read_text(encoding='utf-8'))
        self.assertEqual(survey_roster.ingest(self.db, batch)['cases'][0]['invited'], 2)
        self.assertEqual(survey_roster.ingest(self.db, batch)['imported'], 0)
        survey.ingest(self.db, {'responses': [{
            'event_id': 'r-1', 'course_id': 'C3', 'year': 2026,
            'form_id': 'teaching', 'response_token': 'anon-1', 'score': 5,
            'event_time': '2026-03-03T00:00:00Z',
        }]})
        case = survey_roster.decide(self.db, 'C3', 2026, 'teaching')
        self.assertEqual((case['responded'], case['rate']), (1, 0.5))


if __name__ == '__main__':
    unittest.main()
