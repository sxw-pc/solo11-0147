import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from curriculum import catalog, outline, attestation
from curriculum.store import connect


def fixture(name):
    return json.loads((ROOT / "fixtures" / "curriculum" / name).read_text(encoding="utf-8"))


def succession(event_id, course, old_event, new_event, old_obj, signers, claims,
               event_time="2027-01-10T00:00:00Z"):
    return {
        "event_id": event_id, "op": "succession", "course_id": course,
        "old_outline_event": old_event, "new_outline_event": new_event,
        "old_objective_id": old_obj, "event_time": event_time,
        "signers": signers, "claims": claims,
    }


def approved_pair(a="reviewer-a", b="reviewer-b"):
    return [
        {"signer": a, "decision": "approved", "signed_at": "2027-01-10T00:00:00Z"},
        {"signer": b, "decision": "approved", "signed_at": "2027-01-11T00:00:00Z"},
    ]


def claim(new_obj, subs):
    return {"new_objective_id": new_obj, "covered_subrequirements": subs}


def mandate(mid, course, kind, old_event, new_event):
    return {
        "event_id": "ev-" + mid, "op": "mandate", "mandate_id": mid,
        "course_id": course, "kind": kind,
        "old_outline_event": old_event, "new_outline_event": new_event,
        "event_time": "2027-01-20T00:00:00Z",
    }


class AttestationCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / "test.sqlite")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def load_acceptance_sample(self):
        catalog.ingest(self.db, fixture("attest_catalog.json"))
        outline.ingest(self.db, fixture("attest_outlines.json"))
        outline.ingest(self.db, fixture("attest_succession.json"))
        outline.ingest(self.db, fixture("attest_mandates.json"))

    def result_for(self, objective_id, result):
        return next(o for o in result["detail"]["objectives"]
                    if o["old_objective_id"] == objective_id)

    # 1. Same inputs, two mandates, different decisions.
    def test_two_mandates_same_evidence_different_decisions(self):
        self.load_acceptance_sample()
        cont = attestation.attest(self.db, "mandate-c9-continuation")
        sub = attestation.attest(self.db, "mandate-c9-substitution")
        self.assertEqual(cont["decision"], "approved")
        self.assertEqual(cont["detail"]["scope"], "mandatory_only")
        self.assertEqual([o["old_objective_id"] for o in cont["detail"]["objectives"]], ["M"])
        # Substitution cannot hide behind the mandatory flag: E has no
        # succession statement at all.
        self.assertEqual(sub["decision"], "rejected")
        self.assertEqual(sub["reason"], "coverage_incomplete")
        self.assertEqual(sub["detail"]["scope"], "all_objectives")
        e_result = self.result_for("E", sub)
        self.assertEqual(e_result["status"], "no_approved_succession")
        self.assertTrue(e_result["mandatory"] is False)
        m_result = self.result_for("M", sub)
        self.assertEqual(m_result["status"], "covered")
        self.assertEqual(m_result["required_subrequirements"], ["建模", "质量评估"])

    # 2. Mandate identity, both outline event ids, signers and per-objective
    # evidence all land in the ledgers.
    def test_mandate_and_evidence_are_persisted(self):
        self.load_acceptance_sample()
        stored = self.db.execute(
            "SELECT * FROM attestation_mandate WHERE mandate_id=?",
            ("mandate-c9-substitution",),
        ).fetchone()
        self.assertEqual((stored["kind"], stored["old_outline_event"],
                          stored["new_outline_event"]),
                         ("substitution", "o-c9-2026", "o-c9-2027"))
        stmt = self.db.execute(
            "SELECT * FROM succession_revision WHERE event_id='s-c9-m'"
        ).fetchone()
        self.assertEqual(stmt["old_objective_id"], "M")
        signers = {s["signer"] for s in json.loads(stmt["signers_json"])}
        self.assertEqual(signers, {"reviewer-a", "reviewer-b"})
        case = attestation.current(self.db, "mandate-c9-continuation")
        self.assertEqual(case["decision"], "approved")
        reports = self.db.execute(
            "SELECT COUNT(*) FROM attestation_report WHERE mandate_id=?",
            ("mandate-c9-continuation",),
        ).fetchone()[0]
        self.assertGreaterEqual(reports, 1)
        # Raw outlines remain traceable through the evidence ledger.
        self.assertIsNotNone(
            self.db.execute(
                "SELECT 1 FROM evidence WHERE event_id='o-c9-2026'"
            ).fetchone()
        )

    def _setup_course_c10(self):
        catalog.ingest(self.db, [
            {"event_id": "src-d10", "op": "source", "source_id": "dept-d10",
             "department": "D10", "valid_from": 2025, "valid_until": 2028,
             "event_time": "2026-01-01T00:00:00Z"},
            {"event_id": "c10-2026", "op": "course", "course_id": "C10",
             "department": "D10", "source_id": "dept-d10", "year": 2026,
             "version": 1, "credits": 3, "status": "open",
             "event_time": "2026-01-02T00:00:00Z"},
            {"event_id": "c10-2027", "op": "course", "course_id": "C10",
             "department": "D10", "source_id": "dept-d10", "year": 2027,
             "version": 1, "credits": 3, "status": "open",
             "event_time": "2027-01-02T00:00:00Z"},
        ])
        outline.ingest(self.db, [
            {"event_id": "o-c10-2026", "op": "outline", "course_id": "C10",
             "year": 2026, "version": 1, "issuer": "D10",
             "event_time": "2026-01-03T00:00:00Z",
             "objectives": [
                 {"id": "A", "text": "旧目标A（含 a1、a2 子要求）", "mandatory": True,
                  "subrequirements": ["a1", "a2"]},
                 {"id": "B", "text": "旧目标B（含 b1 子要求）", "mandatory": True,
                  "subrequirements": ["b1"]},
                 {"id": "C", "text": "旧目标C（含 c1、c2 子要求）", "mandatory": True,
                  "subrequirements": ["c1", "c2"]},
             ]},
            {"event_id": "o-c10-2027", "op": "outline", "course_id": "C10",
             "year": 2027, "version": 1, "issuer": "D10",
             "event_time": "2027-01-03T00:00:00Z",
             "objectives": [
                 {"id": "X", "text": "新目标X", "mandatory": True},
                 {"id": "Y", "text": "新目标Y", "mandatory": True},
             ]},
        ])

    # 3. One-to-many split (A -> X+Y) and many-to-one merge (B+C -> X), with
    # full sub-requirement accounting.
    def test_split_one_to_many_and_merge_many_to_one(self):
        self._setup_course_c10()
        outline.ingest(self.db, [
            succession("s-c10-a1", "C10", "o-c10-2026", "o-c10-2027", "A",
                       approved_pair(), [claim("X", ["a1"])]),
            succession("s-c10-a2", "C10", "o-c10-2026", "o-c10-2027", "A",
                       approved_pair("reviewer-c", "reviewer-d"), [claim("Y", ["a2"])]),
            succession("s-c10-bc", "C10", "o-c10-2026", "o-c10-2027", "B",
                       approved_pair("reviewer-e", "reviewer-f"),
                       [claim("X", ["b1"])]),
            succession("s-c10-c", "C10", "o-c10-2026", "o-c10-2027", "C",
                       approved_pair("reviewer-g", "reviewer-h"),
                       [claim("X", ["c1", "c2"])]),
            mandate("mandate-c10", "C10", "continuation",
                    "o-c10-2026", "o-c10-2027"),
        ])
        result = attestation.attest(self.db, "mandate-c10")
        self.assertEqual(result["decision"], "approved")
        statuses = {o["old_objective_id"]: o["status"]
                    for o in result["detail"]["objectives"]}
        self.assertEqual(statuses, {"A": "covered", "B": "covered", "C": "covered"})
        a_result = self.result_for("A", result)
        self.assertEqual(a_result["covered_subrequirements"], ["a1", "a2"])
        # X legitimately serves both B and C because each old objective's own
        # sub-requirement is fully satisfied.
        x_evidenced = {o["old_objective_id"] for o in result["detail"]["objectives"]
                       for e in o["evidence"] if e["new_objective_id"] == "X"}
        self.assertEqual(x_evidenced, {"A", "B", "C"})

    # 4. Same objective id reused in the new version with different content;
    # similar titles never establish coverage without succession evidence.
    def test_same_id_different_meaning_is_not_coverage(self):
        catalog.ingest(self.db, [
            {"event_id": "src-d11", "op": "source", "source_id": "dept-d11",
             "department": "D11", "valid_from": 2025, "valid_until": 2028,
             "event_time": "2026-01-01T00:00:00Z"},
            {"event_id": "c11-2026", "op": "course", "course_id": "C11",
             "department": "D11", "source_id": "dept-d11", "year": 2026,
             "version": 1, "credits": 3, "status": "open",
             "event_time": "2026-01-02T00:00:00Z"},
            {"event_id": "c11-2027", "op": "course", "course_id": "C11",
             "department": "D11", "source_id": "dept-d11", "year": 2027,
             "version": 1, "credits": 3, "status": "open",
             "event_time": "2027-01-02T00:00:00Z"},
        ])
        outline.ingest(self.db, [
            {"event_id": "o-c11-2026", "op": "outline", "course_id": "C11",
             "year": 2026, "version": 1, "issuer": "D11",
             "event_time": "2026-01-03T00:00:00Z",
             "objectives": [
                 {"id": "LO-1", "text": "能够设计关系模型", "mandatory": True}]},
            {"event_id": "o-c11-2027", "op": "outline", "course_id": "C11",
             "year": 2027, "version": 1, "issuer": "D11",
             "event_time": "2027-01-03T00:00:00Z",
             "objectives": [
                 {"id": "LO-1", "text": "能够设计关系模型（方向已改为图数据库）",
                  "mandatory": True}]},
            mandate("mandate-c11", "C11", "continuation",
                    "o-c11-2026", "o-c11-2027"),
        ])
        result = attestation.attest(self.db, "mandate-c11")
        self.assertEqual(result["decision"], "rejected")
        lo1 = self.result_for("LO-1", result)
        self.assertEqual(lo1["status"], "no_approved_succession")
        self.assertEqual(lo1["evidence"], [])

    # 5. A new objective proving only half an ability cannot be double-counted
    # as fully inheriting two old outcomes.
    def test_partial_coverage_rejects_both_split_and_merge(self):
        self._setup_course_c10()
        outline.ingest(self.db, [
            # A splits: only a1 is covered, a2 missing.
            succession("s-c10-a-partial", "C10", "o-c10-2026", "o-c10-2027",
                       "A", approved_pair(), [claim("X", ["a1"])]),
            # B and C merge toward X, but X only proves b1 and c1; C's c2 is
            # absent, so X must not count as full inheritance for both old
            # outcomes.
            succession("s-c10-b-partial", "C10", "o-c10-2026", "o-c10-2027",
                       "B", approved_pair("reviewer-c", "reviewer-d"),
                       [claim("X", ["b1"])]),
            succession("s-c10-c-partial", "C10", "o-c10-2026", "o-c10-2027",
                       "C", approved_pair("reviewer-e", "reviewer-f"),
                       [claim("X", ["c1"])]),
            mandate("mandate-c10-partial", "C10", "continuation",
                    "o-c10-2026", "o-c10-2027"),
        ])
        result = attestation.attest(self.db, "mandate-c10-partial")
        self.assertEqual(result["decision"], "rejected")
        statuses = {o["old_objective_id"]: o["status"]
                    for o in result["detail"]["objectives"]}
        self.assertEqual(statuses["A"], "partial_coverage")
        self.assertEqual(self.result_for("A", result)["missing_subrequirements"], ["a2"])
        self.assertEqual(statuses["B"], "covered")
        self.assertEqual(statuses["C"], "partial_coverage")
        self.assertEqual(self.result_for("C", result)["missing_subrequirements"], ["c2"])

    # 6. Opposed signer opinions force the affected attestation to review.
    def test_signoff_conflict_pends_affected_attestation(self):
        self.load_acceptance_sample()
        split_signers = [
            {"signer": "reviewer-a", "decision": "approved",
             "signed_at": "2027-01-10T00:00:00Z"},
            {"signer": "reviewer-z", "decision": "rejected",
             "signed_at": "2027-01-12T00:00:00Z"},
        ]
        outline.ingest(self.db, [
            succession("s-c9-m-conflict", "C9", "o-c9-2026", "o-c9-2027", "M",
                       split_signers, [claim("N1", ["建模", "质量评估"])]),
        ])
        result = attestation.attest(self.db, "mandate-c9-continuation")
        self.assertEqual(result["decision"], "pending_review")
        self.assertEqual(result["reason"], "signoff_conflict")
        m_result = self.result_for("M", result)
        self.assertEqual(m_result["status"], "signoff_conflict")
        self.assertEqual(m_result["conflicting_statements"], ["s-c9-m-conflict"])
        # The conflict on M does not fabricate coverage for E.
        sub = attestation.attest(self.db, "mandate-c9-substitution")
        self.assertEqual(sub["decision"], "pending_review")

    # 7. Catalog stopping the new academic year pends the case; old-year
    # attestation history and raw outlines stay traceable.
    def test_catalog_stop_pends_while_history_stays_traceable(self):
        self.load_acceptance_sample()
        before = attestation.attest(self.db, "mandate-c9-continuation")
        self.assertEqual(before["decision"], "approved")
        stop = dict(fixture("attest_catalog.json")[2],
                    event_id="c9-2027-stop", version=2, status="stopped",
                    event_time="2027-06-01T00:00:00Z")
        catalog.ingest(self.db, [stop])
        after = attestation.attest(self.db, "mandate-c9-continuation")
        self.assertEqual(after["decision"], "pending_review")
        self.assertIn("catalog_stopped", after["reason"])
        # Append-only history keeps the original approval.
        history = self.db.execute(
            "SELECT decision FROM attestation_report WHERE mandate_id=? "
            "ORDER BY id", ("mandate-c9-continuation",),
        ).fetchall()
        self.assertEqual(history[0]["decision"], "approved")
        self.assertEqual(history[-1]["decision"], "pending_review")
        # Old-year catalog release and both raw outlines remain available.
        self.assertEqual(catalog.current(self.db, "C9", 2026)["decision"], "published")
        self.assertEqual(
            outline.current(self.db, "C9", 2026)["decision"], "published"
        )
        self.assertEqual(
            len(self.db.execute(
                "SELECT * FROM outline_revision WHERE course_id='C9'"
            ).fetchall()),
            2,
        )

    # 8. Replaying the same events yields the same decisions and never
    # duplicates ledger rows.
    def test_idempotent_replay(self):
        self.load_acceptance_sample()
        first = attestation.attest(self.db, "mandate-c9-continuation")
        outline.ingest(self.db, fixture("attest_succession.json"))
        outline.ingest(self.db, fixture("attest_mandates.json"))
        second = attestation.attest(self.db, "mandate-c9-continuation")
        self.assertEqual(second["decision"], first["decision"])
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM succession_revision"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM attestation_mandate"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM evidence WHERE event_id='s-c9-m'"
            ).fetchone()[0],
            1,
        )

    # 9. Import order must not determine the conclusion.
    def test_import_order_does_not_change_conclusion(self):
        catalog.ingest(self.db, fixture("attest_catalog.json"))
        # Mandates and succession arrive before the outlines they reference.
        outline.ingest(self.db, fixture("attest_mandates.json"))
        outline.ingest(self.db, fixture("attest_succession.json"))
        outline.ingest(self.db, fixture("attest_outlines.json"))
        cont = attestation.attest(self.db, "mandate-c9-continuation")
        sub = attestation.attest(self.db, "mandate-c9-substitution")
        self.assertEqual(cont["decision"], "approved")
        self.assertEqual(sub["decision"], "rejected")
        # Shuffled replay of every event in reverse stays stable.
        all_events = (fixture("attest_mandates.json")
                      + fixture("attest_succession.json")
                      + list(reversed(fixture("attest_outlines.json"))))
        outline.ingest(self.db, all_events)
        self.assertEqual(
            attestation.attest(self.db, "mandate-c9-continuation")["decision"],
            "approved",
        )
        self.assertEqual(
            attestation.attest(self.db, "mandate-c9-substitution")["decision"],
            "rejected",
        )

    # 10. A newer outline version supersedes the one the signers referenced.
    def test_superseded_referenced_outline_pends_review(self):
        self.load_acceptance_sample()
        newer = dict(fixture("attest_outlines.json")[1],
                     event_id="o-c9-2027-v2", version=2,
                     event_time="2027-03-03T00:00:00Z")
        outline.ingest(self.db, [newer])
        self.assertEqual(
            outline.current(self.db, "C9", 2027)["event_id"], "o-c9-2027-v2"
        )
        result = attestation.attest(self.db, "mandate-c9-continuation")
        self.assertEqual(result["decision"], "pending_review")
        self.assertIn("referenced_outline_superseded", result["reason"])
        # Replay stays deterministically pending; the superseded raw outline
        # remains on record for traceability.
        again = attestation.attest(self.db, "mandate-c9-continuation")
        self.assertEqual(again["decision"], "pending_review")
        self.assertIsNotNone(
            self.db.execute(
                "SELECT 1 FROM outline_revision WHERE event_id='o-c9-2027'"
            ).fetchone()
        )

    # 11. Claims pointing at objectives absent from the new outline are
    # invalid evidence, never silently accepted.
    def test_claim_to_unknown_new_objective_is_invalid(self):
        self._setup_course_c10()
        # When the outlines are already on record, ingestion rejects outright.
        with self.assertRaises(ValueError):
            outline.ingest(self.db, [
                succession("s-c10-a-ghost", "C10", "o-c10-2026", "o-c10-2027",
                           "A", approved_pair(),
                           [claim("GHOST", ["a1", "a2"])])
            ])
        # When the statement arrives before the outlines it is retained, but
        # attestation still treats a dangling reference as invalid evidence.
        fresh = connect(Path(self.tmp.name) / "ghost.sqlite")
        try:
            catalog.ingest(fresh, [
                {"event_id": "src-d10", "op": "source", "source_id": "dept-d10",
                 "department": "D10", "valid_from": 2025, "valid_until": 2028,
                 "event_time": "2026-01-01T00:00:00Z"},
                {"event_id": "c10-2026", "op": "course", "course_id": "C10",
                 "department": "D10", "source_id": "dept-d10", "year": 2026,
                 "version": 1, "credits": 3, "status": "open",
                 "event_time": "2026-01-02T00:00:00Z"},
                {"event_id": "c10-2027", "op": "course", "course_id": "C10",
                 "department": "D10", "source_id": "dept-d10", "year": 2027,
                 "version": 1, "credits": 3, "status": "open",
                 "event_time": "2027-01-02T00:00:00Z"},
            ])
            outline.ingest(fresh, [
                succession("s-ghost-late", "C10", "o-c10-2026", "o-c10-2027",
                           "A", approved_pair(),
                           [claim("GHOST", ["a1", "a2"])]),
                mandate("mandate-ghost", "C10", "continuation",
                        "o-c10-2026", "o-c10-2027"),
            ])
            outline.ingest(fresh, [
                {"event_id": "o-c10-2026", "op": "outline", "course_id": "C10",
                 "year": 2026, "version": 1, "issuer": "D10",
                 "event_time": "2026-01-03T00:00:00Z",
                 "objectives": [
                     {"id": "A", "text": "旧目标A", "mandatory": True,
                      "subrequirements": ["a1", "a2"]}]},
                {"event_id": "o-c10-2027", "op": "outline", "course_id": "C10",
                 "year": 2027, "version": 1, "issuer": "D10",
                 "event_time": "2027-01-03T00:00:00Z",
                 "objectives": [
                     {"id": "X", "text": "新目标X", "mandatory": True}]},
            ])
            result = attestation.attest(fresh, "mandate-ghost")
            self.assertEqual(result["decision"], "rejected")
            a_result = next(o for o in result["detail"]["objectives"]
                            if o["old_objective_id"] == "A")
            self.assertEqual(a_result["status"], "evidence_not_in_new_outline")
            self.assertEqual(a_result["evidence"], [])
            self.assertEqual(len(a_result["invalid_evidence"]), 1)
        finally:
            fresh.close()

    def test_succession_requires_dual_sign_off_and_real_claims(self):
        self._setup_course_c10()
        with self.assertRaises(ValueError):
            outline.ingest(self.db, [
                succession("s-bad-signers", "C10", "o-c10-2026", "o-c10-2027",
                           "A", [{"signer": "solo", "decision": "approved",
                                  "signed_at": "2027-01-10T00:00:00Z"}],
                           [claim("X", ["a1"])])
            ])
        with self.assertRaises(ValueError):
            outline.ingest(self.db, [
                succession("s-bad-claims", "C10", "o-c10-2026", "o-c10-2027",
                           "A", approved_pair(), [])
            ])
        with self.assertRaises(ValueError):
            outline.ingest(self.db, [
                succession("s-ghost-ref", "C10", "o-c10-2026", "o-c10-2027",
                           "A", approved_pair(), [claim("NOPE", ["a1"])])
            ])


if __name__ == "__main__":
    unittest.main()
