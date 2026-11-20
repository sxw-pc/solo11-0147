import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from curriculum import catalog, outline, assessment
from curriculum.store import connect, rows

COURSE = "C3"
YEAR = 2026
OBJECTIVE = "LO-1"


def fixture(name):
    return json.loads((ROOT / "fixtures" / "curriculum" / name).read_text(encoding="utf-8"))


def score(event_id, assessor, paper_id, value, rubric=1, status="active",
          student=None, objective=OBJECTIVE, when="2026-05-01T00:00:00Z"):
    return {
        "event_id": event_id,
        "op": "assessment",
        "student_id": student if student is not None else f"student-{paper_id}",
        "course_id": COURSE,
        "year": YEAR,
        "objective_id": objective,
        "rubric_version": rubric,
        "score": value,
        "assessor": assessor,
        "paper_id": paper_id,
        "status": status,
        "event_time": when,
    }


def calibration(event_id, paper_id, official, rubric=1, issuer="D",
                when="2026-04-01T00:00:00Z"):
    return {
        "event_id": event_id,
        "op": "anchor_calibration",
        "course_id": COURSE,
        "year": YEAR,
        "objective_id": OBJECTIVE,
        "paper_id": paper_id,
        "rubric_version": rubric,
        "official_score": official,
        "issuer": issuer,
        "event_time": when,
    }


def outline_v2(event_id="o-c3-v2", version=2, when="2026-02-01T00:00:00Z"):
    first = fixture("outline_good.json")[0]
    return [dict(first, event_id=event_id, version=version, event_time=when)]


class BatchReviewCases(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_dir = tempfile.TemporaryDirectory()
        self.db = connect(Path(temporary_dir.name) / "test.sqlite")
        catalog.ingest(self.db, fixture("catalog_good.json"))
        outline.ingest(self.db, fixture("outline_good.json"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def history(self, objective=OBJECTIVE):
        return assessment.review_history(self.db, COURSE, YEAR, objective)

    def test_80_70_sample_splits_internal_and_external_decisions(self):
        assessment.ingest(self.db, [
            score("s-a-p1", "teacher-A", "paper-1", 80),
            score("s-b-p1", "teacher-B", "paper-1", 80),
            score("s-a-p2", "teacher-A", "paper-2", 80),
            score("s-b-p2", "teacher-B", "paper-2", 80),
        ])
        assessment.calibrate(self.db, [
            calibration("cal-p1", "paper-1", 70),
            calibration("cal-p2", "paper-2", 70),
        ])
        # The sign-off must not rely on per-student attained results at all.
        self.db.execute("DELETE FROM assessment_case")
        report = assessment.review_batch(self.db, COURSE, YEAR, OBJECTIVE, 5)

        self.assertEqual(report["internal_review"]["decision"], "publishable")
        self.assertEqual(report["external_certification"]["decision"], "withheld")
        self.assertIn(
            "systematic_bias_out_of_tolerance",
            report["external_certification"]["reasons"],
        )
        # Both decisions share one evidence base: raw scores, anchor identity,
        # official values and the outline objective version.
        self.assertEqual(report["outline"], {"release_event": "o-c3-v1", "version": 1})
        anchors = {a["paper_id"]: a for a in report["anchor_papers"]}
        self.assertEqual(set(anchors), {"paper-1", "paper-2"})
        for anchor in anchors.values():
            self.assertEqual(anchor["official_score"], 70)
            self.assertEqual(
                anchor["calibration_event"],
                f"cal-{anchor['paper_id'].replace('paper-', 'p')}",
            )
            self.assertEqual({s["score"] for s in anchor["scores"]}, {80})
        self.assertEqual(
            set(report["raw_score_events"]),
            {"s-a-p1", "s-b-p1", "s-a-p2", "s-b-p2"},
        )
        biases = {
            (b["assessor"], b["rubric_version"]): b["bias"]
            for component in report["components"]
            for b in component["bias"]
        }
        self.assertEqual(biases, {("teacher-A", 1): 10.0, ("teacher-B", 1): 10.0})

    def test_indirect_connection_certifies_but_disconnected_component_waits(self):
        assessment.ingest(self.db, [
            # paper-1 is the only official anchor and only A has scored it.
            score("s-a-p1", "teacher-A", "paper-1", 70),
            score("s-a-p2", "teacher-A", "paper-2", 70),
            score("s-b-p2", "teacher-B", "paper-2", 71),
            score("s-b-p3", "teacher-B", "paper-3", 71),
            score("s-c-p3", "teacher-C", "paper-3", 72),
            score("s-d-p4", "teacher-D", "paper-4", 70),
            score("s-e-p4", "teacher-E", "paper-4", 70),
        ])
        assessment.calibrate(self.db, [
            calibration("cal-p1", "paper-1", 70),
        ])
        report = assessment.review_batch(self.db, COURSE, YEAR, OBJECTIVE, 5)

        self.assertEqual(report["internal_review"]["decision"], "publishable")
        self.assertEqual(report["external_certification"]["decision"], "withheld")
        self.assertIn(
            "component_without_official_anchor",
            report["external_certification"]["reasons"],
        )
        statuses = {
            tuple(sorted(n["assessor"] for n in c["nodes"])): c["status"]
            for c in report["components"]
        }
        self.assertEqual(
            statuses[("teacher-A", "teacher-B", "teacher-C")], "calibrated"
        )
        self.assertEqual(statuses[("teacher-D", "teacher-E")], "uncalibrated")
        # B and C never touched an official anchor; their bias is propagated
        # from A across the uncalibrated shared papers, including the third hop.
        chain = {
            b["assessor"]: b
            for c in report["components"]
            for b in c["bias"]
            if "teacher-C" in {n["assessor"] for n in c["nodes"]}
        }
        self.assertTrue(chain["teacher-A"]["directly_calibrated"])
        self.assertFalse(chain["teacher-B"]["directly_calibrated"])
        self.assertFalse(chain["teacher-C"]["directly_calibrated"])
        self.assertEqual(chain["teacher-B"]["bias"], 1.0)
        self.assertEqual(chain["teacher-C"]["bias"], 2.0)

    def test_positive_and_negative_bias_cannot_cancel_per_marker(self):
        assessment.ingest(self.db, [
            # C is the calibrated root; B sits one hop away, A two hops away.
            # Internal deltas are all <= 8, but propagated bias accumulates.
            score("s-c-p1", "teacher-C", "paper-1", 70),
            score("s-b-p1", "teacher-B", "paper-1", 78),
            score("s-b-p2", "teacher-B", "paper-2", 70),
            score("s-a-p2", "teacher-A", "paper-2", 78),
            score("s-c-p3", "teacher-C", "paper-3", 70),
            score("s-d-p3", "teacher-D", "paper-3", 62),
        ])
        assessment.calibrate(self.db, [
            calibration("cal-p1", "paper-1", 70),
            calibration("cal-p3", "paper-3", 70),
        ])
        report = assessment.review_batch(self.db, COURSE, YEAR, OBJECTIVE, 9)

        # Every shared-anchor pair stays within tolerance, so internal review
        # passes even though A's propagated bias is 16 and D's is -8.
        self.assertEqual(report["internal_review"]["decision"], "publishable")
        self.assertEqual(report["external_certification"]["decision"], "withheld")
        bias = {
            b["assessor"]: b["bias"]
            for c in report["components"]
            for b in c["bias"]
        }
        self.assertEqual(bias["teacher-A"], 16.0)
        self.assertEqual(bias["teacher-B"], 8.0)
        self.assertEqual(bias["teacher-C"], 0.0)
        self.assertEqual(bias["teacher-D"], -8.0)
        over = {
            n["assessor"]
            for c in report["components"]
            for n in c["over_tolerance"]
        }
        self.assertEqual(over, {"teacher-A"})
        # The group mean (16+8+0-8)/4 = 4 would have hidden the outlier: prove
        # the report keeps per-marker evidence instead of one averaged number.
        self.assertTrue(all("bias" in b for c in report["components"] for b in c["bias"]))

    def test_outline_version_change_reaudits_only_signed_batches(self):
        assessment.ingest(self.db, [
            score("s-a-lo1", "teacher-A", "paper-1", 70),
            score("s-b-lo1", "teacher-B", "paper-1", 70),
            score("s-a-lo2", "teacher-A", "paper-9", 70, objective="LO-2"),
            score("s-b-lo2", "teacher-B", "paper-9", 70, objective="LO-2"),
        ])
        assessment.calibrate(self.db, [
            calibration("cal-p1", "paper-1", 70),
            dict(calibration("cal-p9", "paper-9", 70), objective_id="LO-2"),
        ])
        first = assessment.review_batch(self.db, COURSE, YEAR, OBJECTIVE, 5)
        self.assertEqual(first["external_certification"]["decision"], "certified")
        self.assertEqual(len(self.history()), 1)
        # LO-2 has never been signed: an outline change must not fabricate a row.
        self.assertEqual(self.history("LO-2"), [])

        outline.ingest(self.db, outline_v2())
        self.assertEqual(outline.current(self.db, COURSE, YEAR)["version"], 2)

        history = self.history()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["outline_version"], 1)
        self.assertEqual(history[1]["outline_version"], 2)
        self.assertEqual(
            (history[0]["external_decision"], history[1]["external_decision"]),
            ("certified", "certified"),
        )
        self.assertEqual(self.history("LO-2"), [])
        # The re-audit cites the new outline revision but the same raw scores.
        latest = assessment.review_batch(self.db, COURSE, YEAR, OBJECTIVE, 5)
        self.assertEqual(latest["outline"]["version"], 2)
        self.assertIn("s-a-lo1", latest["raw_score_events"])

    def test_anchor_revocation_disconnects_and_resubmission_restores(self):
        assessment.ingest(self.db, [
            score("s-a-p1", "teacher-A", "paper-1", 70),
            score("s-b-p1", "teacher-B", "paper-1", 70),
            score("s-b-p2", "teacher-B", "paper-2", 70),
            score("s-a-p4", "teacher-A", "paper-4", 70),
        ])
        assessment.calibrate(self.db, [calibration("cal-p1", "paper-1", 70)])
        first = assessment.review_batch(self.db, COURSE, YEAR, OBJECTIVE, 5)
        self.assertEqual(first["external_certification"]["decision"], "certified")

        assessment.ingest(self.db, [
            score("s-a-p1-off", "teacher-A", "paper-1", 70, status="revoked",
                  when="2026-05-02T00:00:00Z"),
        ])
        self.assertEqual(len(self.history()), 2)
        cut = self.history()[-1]
        self.assertEqual(cut["internal_decision"], "publishable")
        self.assertEqual(cut["external_decision"], "withheld")
        self.assertEqual(
            json.loads(cut["reason_json"])["external"],
            ["component_without_official_anchor"],
        )
        report = json.loads(
            self.db.execute(
                "SELECT body FROM batch_review_history WHERE id=?",
                (cut["id"],),
            ).fetchone()["body"]
        )
        self.assertEqual(
            sorted(r["event_id"] for r in report["excluded_evidence"]["revoked"]),
            ["s-a-p1", "s-a-p1-off"],
        )
        # A keeps paper-4 but loses every shared-anchor edge: its component can
        # no longer reach the official anchor, while B stays calibrated.
        self.assertEqual(
            [{n["assessor"] for n in c["nodes"]} for c in report["components"]],
            [{"teacher-A"}, {"teacher-B"}],
        )
        statuses = {
            tuple(sorted(n["assessor"] for n in c["nodes"])): c["status"]
            for c in report["components"]
        }
        self.assertEqual(statuses[("teacher-A",)], "uncalibrated")
        self.assertEqual(statuses[("teacher-B",)], "calibrated")
        # Both markers are now single-node components; only A lost the path to
        # the official anchor, and that is what blocks external certification.
        self.assertEqual(
            {n["assessor"] for n in report["isolated_nodes"]},
            {"teacher-A", "teacher-B"},
        )

        # A fresh, valid submission of the same answer sheet reconnects A;
        # ingest itself re-audits the already-signed batch.
        ingest_result = assessment.ingest(self.db, [
            score("s-a-p1-back", "teacher-A", "paper-1", 70,
                  when="2026-05-03T00:00:00Z"),
        ])
        restored = ingest_result["reviews"][0]
        self.assertEqual(
            (
                restored["internal_review"]["decision"],
                restored["external_certification"]["decision"],
            ),
            ("publishable", "certified"),
        )
        # Every sign-off remains traceable in order: certified -> withheld ->
        # certified, and all original scoring rows are still on the ledger.
        self.assertEqual(
            [
                (h["internal_decision"], h["external_decision"])
                for h in self.history()
            ],
            [
                ("publishable", "certified"),
                ("publishable", "withheld"),
                ("publishable", "certified"),
            ],
        )
        raw = {
            r["event_id"]: r["status"]
            for r in rows(
                self.db,
                "SELECT event_id,status FROM assessment_evidence ORDER BY event_id",
            )
        }
        self.assertEqual(
            raw,
            {
                "s-a-p1": "revoked",
                "s-a-p1-off": "revoked",
                "s-a-p1-back": "active",
                "s-a-p4": "active",
                "s-b-p1": "active",
                "s-b-p2": "active",
            },
        )

    def test_duplicate_resubmission_is_excluded_but_kept_as_evidence(self):
        assessment.ingest(self.db, [
            score("s-a-p1", "teacher-A", "paper-1", 70),
            score("s-b-p1", "teacher-B", "paper-1", 70),
        ])
        assessment.calibrate(self.db, [calibration("cal-p1", "paper-1", 70)])
        assessment.review_batch(self.db, COURSE, YEAR, OBJECTIVE, 5)

        assessment.ingest(self.db, [
            score("s-a-p1-dup", "teacher-A", "paper-1", 99,
                  when="2026-05-04T00:00:00Z"),
        ])
        row = self.db.execute(
            "SELECT status FROM assessment_evidence WHERE event_id='s-a-p1-dup'"
        ).fetchone()
        self.assertEqual(row["status"], "duplicate")
        report = assessment.review_batch(self.db, COURSE, YEAR, OBJECTIVE, 5)
        self.assertEqual(
            report["excluded_evidence"]["duplicates"][0]["event_id"], "s-a-p1-dup"
        )
        # The wild 99 duplicate never enters statistics: still certified.
        self.assertEqual(report["external_certification"]["decision"], "certified")
        anchor = report["anchor_papers"][0]
        self.assertEqual(
            sorted(s["event_id"] for s in anchor["scores"]), ["s-a-p1", "s-b-p1"]
        )
        # Raw evidence remains traceable, including the excluded submission.
        self.assertIn("s-a-p1-dup", report["raw_score_events"])

    def test_rubric_version_switch_splits_marker_into_new_component(self):
        assessment.ingest(self.db, [
            score("s-a-p1", "teacher-A", "paper-1", 70, rubric=1),
            score("s-b-p1", "teacher-B", "paper-1", 70, rubric=1),
            score("s-a-p2", "teacher-A", "paper-2", 75, rubric=2,
                  when="2026-05-05T00:00:00Z"),
        ])
        assessment.calibrate(self.db, [calibration("cal-p1", "paper-1", 70, rubric=1)])
        report = assessment.review_batch(self.db, COURSE, YEAR, OBJECTIVE, 5)

        self.assertEqual(report["internal_review"]["decision"], "publishable")
        self.assertEqual(report["external_certification"]["decision"], "withheld")
        nodes = {
            (n["assessor"], n["rubric_version"])
            for c in report["components"]
            for n in c["nodes"]
        }
        self.assertEqual(nodes, {("teacher-A", 1), ("teacher-B", 1), ("teacher-A", 2)})
        # No pairwise check may compare different rubric versions.
        self.assertTrue(
            all(
                {p["assessor_a"], p["assessor_b"]} != {"teacher-A", "teacher-A"}
                or len({p["rubric_version"]}) == 1
                for p in report["pairwise_checks"]
            )
        )
        uncalibrated = [c for c in report["components"] if c["status"] == "uncalibrated"]
        self.assertEqual(
            [(n["assessor"], n["rubric_version"]) for n in uncalibrated[0]["nodes"]],
            [("teacher-A", 2)],
        )

    def test_personal_decisions_ignore_revoked_and_keep_original_semantics(self):
        assessment.ingest(self.db, [
            score("s-1", "teacher-A", "paper-1", 65, student="lonely-student"),
        ])
        personal = assessment.decide(self.db, "lonely-student", COURSE, YEAR, OBJECTIVE)
        self.assertEqual(personal["decision"], "not_attained")

        assessment.ingest(self.db, [
            score("s-1-off", "teacher-A", "paper-1", 65, student="lonely-student",
                  status="revoked", when="2026-05-06T00:00:00Z"),
        ])
        personal = assessment.decide(self.db, "lonely-student", COURSE, YEAR, OBJECTIVE)
        self.assertEqual(personal["decision"], "missing")
        self.assertIsNone(personal["source_event"])

        assessment.ingest(self.db, [
            score("s-1-new", "teacher-A", "paper-1", 80, student="lonely-student",
                  when="2026-05-07T00:00:00Z"),
        ])
        personal = assessment.decide(self.db, "lonely-student", COURSE, YEAR, OBJECTIVE)
        self.assertEqual(
            (personal["decision"], personal["source_event"]),
            ("attained", "s-1-new"),
        )

    def test_calibration_revision_reopens_signed_batch(self):
        assessment.ingest(self.db, [
            score("s-a-p1", "teacher-A", "paper-1", 72),
            score("s-b-p1", "teacher-B", "paper-1", 72),
        ])
        assessment.calibrate(self.db, [calibration("cal-p1", "paper-1", 70)])
        first = assessment.review_batch(self.db, COURSE, YEAR, OBJECTIVE, 5)
        self.assertEqual(first["external_certification"]["decision"], "certified")

        # The official ruler moves against the group without any new scores:
        # the existing sign-off must be reopened automatically.
        assessment.calibrate(self.db, [
            calibration("cal-p1-v2", "paper-1", 80, when="2026-04-05T00:00:00Z"),
        ])
        history = self.history()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1]["external_decision"], "withheld")
        report = assessment.review_batch(self.db, COURSE, YEAR, OBJECTIVE, 5)
        anchor = report["anchor_papers"][0]
        self.assertEqual(
            (anchor["official_score"], anchor["calibration_event"]),
            (80, "cal-p1-v2"),
        )
        bias = {
            b["assessor"]: b["bias"]
            for c in report["components"]
            for b in c["bias"]
        }
        self.assertEqual(bias, {"teacher-A": -8.0, "teacher-B": -8.0})

    def test_pairwise_violation_blocks_internal_and_external(self):
        assessment.ingest(self.db, [
            score("s-a-p1", "teacher-A", "paper-1", 80),
            score("s-b-p1", "teacher-B", "paper-1", 70),
        ])
        assessment.calibrate(self.db, [calibration("cal-p1", "paper-1", 70)])
        report = assessment.review_batch(self.db, COURSE, YEAR, OBJECTIVE, 5)
        self.assertEqual(report["internal_review"]["decision"], "withheld")
        self.assertEqual(
            report["internal_review"]["reason"], "pairwise_tolerance_exceeded"
        )
        self.assertEqual(report["external_certification"]["decision"], "withheld")
        self.assertIn(
            "internal_review_failed", report["external_certification"]["reasons"]
        )
        self.assertEqual(report["violations"][0]["difference"], 10)

    def test_only_affected_batch_is_reaudited(self):
        assessment.ingest(self.db, [
            score("s-a-lo1", "teacher-A", "paper-1", 70),
            score("s-b-lo1", "teacher-B", "paper-1", 70),
            score("s-a-lo2", "teacher-A", "paper-9", 70, objective="LO-2"),
            score("s-b-lo2", "teacher-B", "paper-9", 70, objective="LO-2"),
        ])
        assessment.calibrate(self.db, [
            calibration("cal-p1", "paper-1", 70),
            dict(calibration("cal-p9", "paper-9", 70), objective_id="LO-2"),
        ])
        assessment.review_batch(self.db, COURSE, YEAR, OBJECTIVE, 5)
        assessment.review_batch(self.db, COURSE, YEAR, "LO-2", 5)

        assessment.ingest(self.db, [
            score("s-a-p1-off", "teacher-A", "paper-1", 70, status="revoked",
                  when="2026-05-08T00:00:00Z"),
        ])
        self.assertEqual(len(self.history(OBJECTIVE)), 2)
        self.assertEqual(len(self.history("LO-2")), 1)


if __name__ == "__main__":
    unittest.main()
