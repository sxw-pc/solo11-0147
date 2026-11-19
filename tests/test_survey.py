import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from curriculum import catalog, survey
from curriculum.store import connect


def fixture():
    return json.loads((ROOT / "fixtures" / "curriculum" / "survey_good.json").read_text(encoding="utf-8"))


class SurveyCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / "test.sqlite")
        catalog.ingest(self.db, json.loads((ROOT / "fixtures" / "curriculum" / "catalog_good.json").read_text(encoding="utf-8")))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_survey_is_independent_from_student_qualification(self):
        result = survey.ingest(self.db, fixture())
        self.assertEqual(result["cases"][0]["responses"], 2)
        self.assertEqual(result["cases"][0]["mean_score"], 4.5)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM qualification_case").fetchone()[0], 0)
        self.assertEqual(survey.ingest(self.db, fixture())["imported"], 0)

    def test_response_correction_uses_latest_token(self):
        survey.ingest(self.db, fixture())
        changed = dict(fixture()["responses"][0], event_id="r1-v2", score=1, event_time="2026-06-01T00:00:00Z")
        result = survey.ingest(self.db, {"responses": [changed]})
        self.assertEqual(result["cases"][0]["mean_score"], 2.5)
        self.assertEqual(result["cases"][0]["responses"], 2)

    def test_unpublished_form_and_event_conflict(self):
        with self.assertRaises(ValueError):
            survey.ingest(self.db, {"responses": fixture()["responses"]})
        survey.ingest(self.db, fixture())
        changed = dict(fixture()["responses"][0], score=1)
        with self.assertRaises(ValueError):
            survey.ingest(self.db, {"responses": [changed]})

