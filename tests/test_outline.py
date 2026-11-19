import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from curriculum import catalog, outline
from curriculum.store import connect


def fixture(name):
    return json.loads((ROOT / "fixtures" / "curriculum" / name).read_text(encoding="utf-8"))


class OutlineCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / "test.sqlite")
        catalog.ingest(self.db, fixture("catalog_good.json"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_published_outline_is_independent_from_course_qualification(self):
        result = outline.ingest(self.db, fixture("outline_good.json"))
        self.assertEqual(result["imported"], 1)
        current = outline.current(self.db, "C3", 2026)
        self.assertEqual(current["decision"], "published")
        self.assertEqual(len(current["objectives"]), 3)
        self.assertEqual(catalog.current(self.db, "C3", 2026)["decision"], "published")

    def test_same_version_conflict_keeps_evidence_and_disputes_outline(self):
        first = fixture("outline_good.json")[0]
        outline.ingest(self.db, [first])
        conflict = dict(first, event_id="o-c3-conflict", objectives=[dict(first["objectives"][0], text="不同目标")])
        outline.ingest(self.db, [conflict])
        self.assertEqual(outline.current(self.db, "C3", 2026)["decision"], "disputed")
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM outline_revision").fetchone()[0], 2
        )

    def test_untrusted_outline_and_duplicate_event(self):
        first = fixture("outline_good.json")[0]
        bad = dict(first, event_id="o-c3-bad", issuer="other")
        outline.ingest(self.db, [bad])
        self.assertEqual(outline.current(self.db, "C3", 2026)["decision"], "pending")
        outline.ingest(self.db, [first])
        self.assertEqual(outline.ingest(self.db, [first])["imported"], 0)
        with self.assertRaises(ValueError):
            outline.ingest(self.db, [dict(first, objectives=[])])

