import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from curriculum import catalog, outline, assessment
from curriculum.store import connect


def fixture(name):
    return json.loads((ROOT / "fixtures" / "curriculum" / name).read_text(encoding="utf-8"))


class AssessmentCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / "test.sqlite")
        catalog.ingest(self.db, fixture("catalog_good.json"))
        outline.ingest(self.db, fixture("outline_good.json"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_assessment_is_independent_from_course_qualification(self):
        result = assessment.ingest(self.db, fixture("assessment_good.json"))
        self.assertEqual(result["imported"], 2)
        decisions = {x["objective_id"]: x["decision"] for x in result["cases"]}
        self.assertEqual(decisions, {"LO-1": "attained", "LO-2": "not_attained"})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM qualification_case").fetchone()[0], 0)

    def test_new_score_revises_one_objective(self):
        assessment.ingest(self.db, fixture("assessment_good.json"))
        revised = dict(fixture("assessment_good.json")[1], event_id="as-s1-lo2-v2", score=76, event_time="2026-06-01T00:00:00Z")
        result = assessment.ingest(self.db, [revised])
        self.assertEqual(result["cases"][0]["decision"], "attained")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM assessment_evidence").fetchone()[0], 3)

    def test_unknown_objective_and_conflict_are_rejected(self):
        item = fixture("assessment_good.json")[0]
        with self.assertRaises(ValueError):
            assessment.ingest(self.db, [dict(item, event_id="bad", objective_id="UNKNOWN")])
        assessment.ingest(self.db, [item])
        with self.assertRaises(ValueError):
            assessment.ingest(self.db, [dict(item, score=10)])

