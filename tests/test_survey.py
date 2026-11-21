import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from curriculum import catalog, survey
from curriculum.store import connect

CATALOG = json.loads((ROOT / "fixtures" / "curriculum" / "catalog_good.json").read_text(encoding="utf-8"))


def survey_fixture():
    return json.loads((ROOT / "fixtures" / "curriculum" / "survey_good.json").read_text(encoding="utf-8"))


def responses(tokens, start=0, score_of=lambda token: 3):
    return [
        {"event_id": f"r-{token}-{start + index}",
         "course_id": "C3", "year": 2026, "form_id": "eval-1",
         "response_token": token, "score": score_of(token),
         "event_time": "2026-05-02T00:00:00Z"}
        for index, token in enumerate(tokens)
    ]


def form(event_id="f1", version=1, question="对课程总体满意度"):
    return {"event_id": event_id, "course_id": "C3", "year": 2026,
            "form_id": "eval-1", "version": version, "question": question,
            "issuer": "D", "event_time": "2026-05-01T00:00:00Z"}


class SurveyCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / "test.sqlite")
        catalog.ingest(self.db, CATALOG)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def load(self, scores, tokens=None):
        tokens = tokens or [f"t{index}" for index in range(len(scores))]
        scores = dict(zip(tokens, scores))
        survey.ingest(self.db, {"forms": [form()],
                                "responses": responses(tokens, score_of=scores.get)})
        return tokens

    def publish(self, groups, **kwargs):
        return survey.publish(self.db, "C3", 2026, "eval-1", groups, **kwargs)

    def assert_no_tokens_in_public(self, tokens):
        self.load(tokens)  # make the tokens known to the ledger if not yet
        public = json.dumps(survey.public_report(self.db, "C3", 2026, "eval-1"),
                            ensure_ascii=False)
        for token in tokens:
            self.assertNotIn(token, public)

    # -- existing internal-report contract --------------------------------

    def test_survey_is_independent_from_student_qualification(self):
        result = survey.ingest(self.db, survey_fixture())
        self.assertEqual(result["cases"][0]["responses"], 2)
        self.assertEqual(result["cases"][0]["mean_score"], 4.5)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM qualification_case").fetchone()[0], 0)
        self.assertEqual(survey.ingest(self.db, survey_fixture())["imported"], 0)

    def test_response_correction_uses_latest_token(self):
        survey.ingest(self.db, survey_fixture())
        changed = dict(survey_fixture()["responses"][0], event_id="r1-v2", score=1,
                       event_time="2026-06-01T00:00:00Z")
        result = survey.ingest(self.db, {"responses": [changed]})
        self.assertEqual(result["cases"][0]["mean_score"], 2.5)
        self.assertEqual(result["cases"][0]["responses"], 2)

    def test_unpublished_form_and_event_conflict(self):
        with self.assertRaises(ValueError):
            survey.ingest(self.db, {"responses": survey_fixture()["responses"]})
        survey.ingest(self.db, survey_fixture())
        changed = dict(survey_fixture()["responses"][0], score=1)
        with self.assertRaises(ValueError):
            survey.ingest(self.db, {"responses": [changed]})

    # -- same size, opposite conclusions ---------------------------------

    def test_same_size_groups_differ_by_history(self):
        # scores 5,5,5,1,5.  Two four-person groups share a,b,c.  Under both
        # totals a=b=c=5 is pinned too, but d is not named by the new batch,
        # so the differential is reported on the batch members a,b,c,e; the
        # audit still exposes the whole determined set in ``domains``.
        self.load([5, 5, 5, 1, 5], tokens=["a", "b", "c", "d", "e"])
        first = self.publish([["a", "b", "c", "d"]])
        self.assertEqual((first["decision"], first["public"][0]["total_score"]),
                         ("published", 16))
        second = self.publish([["a", "b", "c", "e"]])
        self.assertEqual((second["decision"], second["reason"]), ("frozen", "inference"))
        self.assertEqual(second["affected_tokens"], ["a", "b", "c", "d", "e"])
        self.assertEqual(second["domains"]["a"], [5])
        self.assertEqual(second["domains"]["e"], [5])
        self.assertIn(1, second["causing_versions"])
        self.assertIn(2, second["causing_versions"])
        # The group list appears in the reviewable constraint summary.
        self.assertEqual({c["version"] for c in second["constraints"]}, {1, 2})
        # d is outside the new group yet the two totals also pin it to 1; the
        # internal domains report it even though it is not re-stated publicly.
        audit = survey.audit(self.db, "C3", 2026, "eval-1")
        self.assertEqual(audit["applications"][1]["domains"]["d"], [1])

        # A different group of exactly four people stays safe: scores are
        # 1,1,1,2,1 so every member keeps 1 or 2 under both overlapping sums.
        self.db.close()
        self.db = connect(Path(self.tmp.name) / "safe.sqlite")
        catalog.ingest(self.db, CATALOG)
        self.load([1, 1, 1, 2, 1], tokens=["a", "b", "c", "d", "e"])
        self.assertEqual(self.publish([["a", "b", "d", "e"]])["decision"], "published")
        safe = self.publish([["a", "b", "c", "d"]])
        self.assertEqual((safe["decision"], safe["reason"]),
                         ("published", "aggregate_released"))

    def test_no_single_query_ever_blocks_on_group_size(self):
        # One group of four with all equal scores is published without any
        # history; a size threshold alone is not the gate.
        self.load([3, 3, 3, 3], tokens=["a", "b", "c", "d"])
        result = self.publish([["a", "b", "c", "d"]])
        self.assertEqual((result["decision"], result["affected_tokens"]), ("published", []))

    # -- three-query combination ------------------------------------------

    def test_third_query_reveals_one_person(self):
        # scores 2,3,4,1,5.  {a,b,c,d}=10 and {a,b}=5 share two people.
        # Adding {c,d,e}=10 forces e=5 even though no pair does.
        self.load([2, 3, 4, 1, 5], tokens=["a", "b", "c", "d", "e"])
        self.assertEqual(self.publish([["a", "b", "c", "d"]])["decision"], "published")
        self.assertEqual(self.publish([["a", "b"]])["decision"], "published")
        third = self.publish([["c", "d", "e"]])
        self.assertEqual((third["decision"], third["reason"]), ("frozen", "inference"))
        self.assertIn("e", third["affected_tokens"])

    # -- rounded mean boundary --------------------------------------------

    def test_rounding_uses_published_precision(self):
        # scores 1,1,1,1,2 => total 6, mean 1.2, rounded to 1 at precision 0.
        # Publishing only the rounded mean keeps totals 5..7 possible, so the
        # release alone constrains nobody; the internal exact mean stays 1.2.
        self.load([1, 1, 1, 1, 2], tokens=["a", "b", "c", "d", "e"])
        internal = survey.decide(self.db, "C3", 2026, "eval-1")
        self.assertAlmostEqual(internal["mean_score"], 6 / 5)
        result = self.publish([["a", "b", "c", "d", "e"]], precision=0, publish_total=False)
        self.assertEqual(result["decision"], "published")
        self.assertEqual(result["public"][0]["mean_score"], 1.0)
        self.assertIsNone(result["public"][0]["total_score"])

        # At the precision actually published, two groups whose rounded means
        # are equal do not imply equal total scores.  {a,b,c,d,e} kept total in
        # 5..7; {a,b,c,d} has scores 1,1,1,1 and rounds to 1.0 too, yet this
        # does not pin e.
        second = self.publish([["a", "b", "c", "d"]], precision=0, publish_total=False)
        self.assertEqual(second["decision"], "published")
        # Re-publishing the same set at full precision as a rounded mean is
        # judged at that finer precision: total 6 over five people rounds to
        # 1.2 and the exact four-person total leaves e free, so it passes and
        # the check never substitutes the internal exact mean of 1.2 for the
        # coarser 1.0 published above.
        third = self.publish([["a", "b", "c", "d", "e"]], precision=1, publish_total=False)
        self.assertEqual(third["decision"], "published")
        self.assertEqual(third["public"][0]["mean_score"], 1.2)

    # -- corrections keep history -----------------------------------------

    def test_correction_keeps_history_and_rechecks(self):
        self.load([5, 5, 5, 1], tokens=["a", "b", "c", "d"])
        self.assertEqual(self.publish([["a", "b", "c", "d"]])["decision"], "published")
        corrected = dict(responses(["d"], score_of=lambda _: 2)[0], event_id="r-d-fix",
                         event_time="2026-06-01T00:00:00Z")
        survey.ingest(self.db, {"responses": [corrected]})
        internal = survey.decide(self.db, "C3", 2026, "eval-1")
        self.assertAlmostEqual(internal["mean_score"], 17 / 4)

        # The historical public value is untouched.
        public = survey.public_report(self.db, "C3", 2026, "eval-1")
        self.assertEqual(public["results"][0]["total_score"], 16)

        # New application re-checks against the current answers.  The old
        # total 16 no longer equals the corrected answers and is stale; the
        # new four-person total 17 does not pin anyone individually.
        app = self.publish([["a", "b", "c", "d"]])
        self.assertEqual(app["decision"], "published")
        self.assertEqual(app["stale_versions"], [1])
        self.assertEqual(app["public"][0]["total_score"], 17)

    # -- catalog freeze and restoration -----------------------------------

    def test_catalog_stop_freezes_publication_until_restored(self):
        # 2026 publication is legal and stays untouched.
        self.load([3, 3, 3, 3], tokens=["a", "b", "c", "d"])
        self.assertEqual(self.publish([["a", "b", "c", "d"]])["decision"], "published")
        # 2027 catalog stops the course.  Existing 2026 answers stay valid.
        stop = dict(CATALOG[3], event_id="c3-stop", year=2027, version=2,
                    status="stopped", event_time="2027-01-01T00:00:00Z")
        catalog.ingest(self.db, [stop])
        frozen = survey.publish(self.db, "C3", 2027, "eval-1", [["a", "b", "c", "d"]])
        self.assertEqual((frozen["decision"], frozen["reason"]),
                         ("frozen", "catalog_stopped"))
        evidence = self.db.execute(
            "SELECT catalog_decision,catalog_event_id FROM survey_catalog_evidence "
            "WHERE course_id='C3' AND year=2027").fetchone()
        self.assertEqual((evidence["catalog_decision"], evidence["catalog_event_id"]),
                         ("stopped", "c3-stop"))
        audit = survey.audit(self.db, "C3", 2027, "eval-1")
        self.assertTrue(audit["catalog_evidence"])

        # Restoration re-permits publication; the frozen application stays in
        # history and the 2026 public value is unchanged.
        reopen = dict(CATALOG[3], event_id="c3-2027-open", year=2027, version=3,
                      status="open", event_time="2027-02-01T00:00:00Z")
        catalog.ingest(self.db, [reopen])
        survey.ingest(self.db, {
            "forms": [dict(form("f-2027"), year=2027, event_time="2027-02-02T00:00:00Z")],
            "responses": [dict(item, event_id=item["event_id"] + "-27", year=2027,
                               event_time="2027-02-03T00:00:00Z")
                          for item in responses(["a", "b", "c", "d"],
                                                score_of=lambda _: 3)],
        })
        recovered = survey.publish(self.db, "C3", 2027, "eval-1", [["a", "b", "c"]])
        self.assertEqual(recovered["decision"], "published")
        audit = survey.audit(self.db, "C3", 2027, "eval-1")
        self.assertEqual([a["decision"] for a in audit["applications"]],
                         ["frozen", "published"])
        self.assertEqual(
            survey.public_report(self.db, "C3", 2026, "eval-1")["results"][0]["total_score"], 12)

    def test_catalog_dispute_freezes_survey(self):
        self.load([3, 3, 3, 3], tokens=["a", "b", "c", "d"])
        conflict = dict(CATALOG[3], event_id="c3-conflict", version=1, credits=5,
                        event_time="2026-06-01T00:00:00Z")
        catalog.ingest(self.db, [conflict])
        result = self.publish([["a", "b", "c", "d"]])
        self.assertEqual((result["decision"], result["reason"]), ("frozen", "catalog_disputed"))
        self.assertEqual(result["affected_tokens"], [])

    # -- form version dispute ---------------------------------------------

    def test_disputed_form_version_freezes_and_reports(self):
        self.load([3, 3, 3, 3], tokens=["a", "b", "c", "d"])
        survey.ingest(self.db, {"forms": [form("f2", version=1, question="不同题目")]})
        result = self.publish([["a", "b", "c", "d"]])
        self.assertEqual((result["decision"], result["reason"]), ("frozen", "form_disputed"))
        # Internal mean is still computed from the effective (latest) events.
        internal = survey.decide(self.db, "C3", 2026, "eval-1")
        self.assertEqual(internal["responses"], 4)
        self.assertAlmostEqual(internal["mean_score"], 3.0)

    # -- duplicate / replayed events --------------------------------------

    def test_duplicate_events_and_replay(self):
        self.load([4, 4], tokens=["a", "b"])
        before = survey.decide(self.db, "C3", 2026, "eval-1")
        event = responses(["a"], score_of=lambda _: 4)[0]
        survey.ingest(self.db, {"responses": [event]})
        after = survey.decide(self.db, "C3", 2026, "eval-1")
        self.assertEqual((before["responses"], before["mean_score"]),
                         (after["responses"], after["mean_score"]))
        # Same event id with different content is still rejected by sources.py.
        with self.assertRaises(ValueError):
            survey.ingest(self.db, {"responses": [dict(event, score=1)]})
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM evidence WHERE event_id=?",
                            (event["event_id"],)).fetchone()[0], 1)

    # -- rounding boundaries ---------------------------------------------

    def test_round_mean_is_half_up_at_published_precision(self):
        from curriculum import inference
        self.assertEqual(float(inference.round_mean(5, 2, 0)), 3.0)   # 2.5 -> 3
        self.assertEqual(float(inference.round_mean(5, 2, 1)), 2.5)
        self.assertEqual(float(inference.round_mean(7, 3, 2)), 2.33)  # 2.333...
        self.assertEqual(float(inference.round_mean(7, 3, 0)), 2.0)   # 2.333 -> 2

    def test_catalog_restoration_still_honours_history(self):
        self.load([5, 5, 5, 1, 5], tokens=["a", "b", "c", "d", "e"])
        # A legal 2026 aggregate is published before the catalog dispute.
        self.assertEqual(self.publish([["a", "b", "c", "d"]])["decision"], "published")
        conflict = dict(CATALOG[3], event_id="c3-conflict-2", version=1, credits=4,
                        event_time="2026-06-01T00:00:00Z")
        catalog.ingest(self.db, [conflict])
        blocked = self.publish([["a", "b", "c", "e"]])
        self.assertEqual((blocked["decision"], blocked["reason"]),
                         ("frozen", "catalog_disputed"))

        # Restoring the catalog re-opens the gate, but the historical public
        # value still takes part in the inference constraint.
        restore = dict(CATALOG[3], event_id="c3-restore", version=2, credits=3,
                       event_time="2026-07-01T00:00:00Z")
        catalog.ingest(self.db, [restore])
        result = self.publish([["a", "b", "c", "e"]])
        self.assertEqual((result["decision"], result["reason"]), ("frozen", "inference"))
        self.assertIn(1, result["causing_versions"])   # the pre-freeze release
        self.assertIn(3, result["causing_versions"])   # the new application
        self.assertIn("e", result["affected_tokens"])
        audit = survey.audit(self.db, "C3", 2026, "eval-1")
        self.assertEqual([a["reason"] for a in audit["applications"]],
                         ["aggregate_released", "catalog_disputed", "inference"])
        self.assertTrue(any(e["catalog_decision"] == "disputed"
                            for e in audit["catalog_evidence"]))

    # -- publication history / public sanitisation ------------------------

    def test_public_report_hides_tokens_and_internal_audit_keeps_them(self):
        self.load([5, 5, 5, 1, 5], tokens=["sec-a", "sec-b", "sec-c", "sec-d", "sec-e"])
        self.publish([["sec-a", "sec-b", "sec-c", "sec-d"]])
        self.publish([["sec-a", "sec-b", "sec-c", "sec-e"]])
        public = survey.public_report(self.db, "C3", 2026, "eval-1")
        self.assertEqual(len(public["results"]), 1)  # second release was frozen
        text = json.dumps(public, ensure_ascii=False)
        for token in ("sec-a", "sec-b", "sec-c", "sec-d", "sec-e"):
            self.assertNotIn(token, text)
        audit = survey.audit(self.db, "C3", 2026, "eval-1")
        self.assertEqual(audit["applications"][1]["affected_tokens"],
                         ["sec-a", "sec-b", "sec-c", "sec-d", "sec-e"])
        # Version/group identifiers are internal too.
        self.assertNotIn("sequence", json.dumps(audit))

    def test_published_version_cannot_be_silently_reused(self):
        self.load([3, 3, 3, 3], tokens=["a", "b", "c", "d"])
        self.publish([["a", "b", "c", "d"]])
        with self.assertRaises(ValueError):
            self.publish([["a", "b"]], version=1)


if __name__ == "__main__":
    unittest.main()
