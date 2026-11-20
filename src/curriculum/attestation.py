"""Cross-version learning-outcome attestation against signed succession evidence.

Two review mandates share the same outline events and the same succession
statements but apply different coverage scopes:

* ``continuation`` — every mandatory objective of the old outline must be
  genuinely covered by the new outline;
* ``substitution`` — the elective objectives must be covered as well, the
  ``mandatory`` flag is never an exemption.

Coverage is never inferred from objective ids or similar titles.  It only
follows succession statements whose claims point at concrete new objective
ids and enumerate the old objective's necessary sub-requirements.  A new
objective that satisfies only part of an old objective cannot be counted as
fully inheriting two old outcomes: each old objective's full sub-requirement
set must be covered, and claims referencing objectives absent from the
currently eligible new outline are invalid evidence.
"""
import json

from .sources import record
from .store import rows
from . import catalog, outline

CONTINUATION = "continuation"
SUBSTITUTION = "substitution"
KINDS = (CONTINUATION, SUBSTITUTION)

APPROVED = "approved"
REJECTED = "rejected"
PENDING_REVIEW = "pending_review"


def ingest(db, events):
    """Record succession statements and attestation mandates as evidence."""
    with db:
        count = record_events(db, events)
        for mandate in rows(db, "SELECT mandate_id FROM attestation_mandate"):
            attest(db, mandate["mandate_id"])
    return {"imported": count, "cases": rows(db, "SELECT * FROM attestation_case")}


def record_events(db, events):
    """Validate and insert events without managing the surrounding transaction.

    Cross-reference checks against the two outline events run only when the
    outlines are already on record; otherwise attestation time handles them,
    so ingestion order never determines the conclusion.
    """
    count = 0
    for event in events:
        op = event.get("op")
        if op == "succession":
            _validate_succession(db, event)
            if record(db, event, "succession", event["course_id"]):
                db.execute(
                    "INSERT INTO succession_revision VALUES (?,?,?,?,?,?,?)",
                    (
                        event["event_id"],
                        event["course_id"],
                        event["old_outline_event"],
                        event["new_outline_event"],
                        event["old_objective_id"],
                        json.dumps(event["signers"], ensure_ascii=False, sort_keys=True),
                        json.dumps(event["claims"], ensure_ascii=False, sort_keys=True),
                    ),
                )
                count += 1
        elif op == "mandate":
            if event["kind"] not in KINDS:
                raise ValueError("unknown attestation mandate kind")
            for field in ("mandate_id", "course_id", "old_outline_event", "new_outline_event"):
                if not event.get(field):
                    raise ValueError(f"mandate requires {field}")
            if record(db, event, "attestation_mandate", event["mandate_id"]):
                db.execute(
                    "INSERT INTO attestation_mandate VALUES (?,?,?,?,?,?)",
                    (
                        event["mandate_id"],
                        event["event_id"],
                        event["course_id"],
                        event["kind"],
                        event["old_outline_event"],
                        event["new_outline_event"],
                    ),
                )
                count += 1
        else:
            raise ValueError("attestation input must be succession or mandate")
    return count


def _validate_succession(db, event):
    for field in ("event_id", "course_id", "old_outline_event", "new_outline_event", "old_objective_id"):
        if not event.get(field):
            raise ValueError(f"succession requires {field}")
    signers = event.get("signers")
    if not isinstance(signers, list) or len(signers) < 2:
        raise ValueError("succession requires dual sign-off")
    if len({s.get("signer") for s in signers}) != len(signers):
        raise ValueError("succession signers must be distinct")
    for signer in signers:
        if signer.get("decision") not in ("approved", "rejected") or not signer.get("signer"):
            raise ValueError("succession signer decision must be approved or rejected")
    claims = event.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ValueError("succession requires at least one claim")
    for claim in claims:
        if not claim.get("new_objective_id"):
            raise ValueError("succession claim requires new_objective_id")
        covered = claim.get("covered_subrequirements")
        if not isinstance(covered, list) or not covered or any(not str(x).strip() for x in covered):
            raise ValueError("succession claim requires distinct covered sub-requirements")
        if len(set(covered)) != len(covered):
            raise ValueError("succession claim requires distinct covered sub-requirements")
    old = _load_outline(db, event["old_outline_event"])
    new = _load_outline(db, event["new_outline_event"])
    # Outlines may be imported later; attest() re-checks reference validity.
    if old is not None and new is not None:
        if old["course_id"] != event["course_id"] or new["course_id"] != event["course_id"]:
            raise ValueError("succession outline events must belong to the same course")
        old_objective = next(
            (o for o in old["objectives"] if o["id"] == event["old_objective_id"]),
            None,
        )
        if old_objective is None:
            raise ValueError("succession old_objective_id must exist in old outline")
        allowed = set(required_subrequirements(old_objective))
        new_ids = {o["id"] for o in new["objectives"]}
        for claim in claims:
            if claim["new_objective_id"] not in new_ids:
                raise ValueError("succession claim must reference an objective in new outline")
            if not set(claim["covered_subrequirements"]) <= allowed:
                raise ValueError(
                    "succession claim sub-requirements must come from the old "
                    "objective's necessary sub-requirements"
                )


def _load_outline(db, event_id):
    row = db.execute(
        "SELECT event_id,course_id,year,version,objectives_json FROM outline_revision WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["objectives"] = json.loads(result.pop("objectives_json"))
    return result


def required_subrequirements(objective):
    """Authoritative necessary sub-requirements declared by the old outline.

    An objective without an explicit ``subrequirements`` list carries one
    implicit sub-requirement keyed by its own id.  Succession statements may
    only claim coverage against this list; they cannot define it themselves.
    """
    declared = objective.get("subrequirements")
    if declared:
        return list(declared)
    return [objective["id"]]


def statements(db, course_id, old_outline_event, new_outline_event):
    out = []
    for row in rows(
        db,
        "SELECT * FROM succession_revision WHERE course_id=? AND old_outline_event=? "
        "AND new_outline_event=? ORDER BY event_id",
        (course_id, old_outline_event, new_outline_event),
    ):
        item = dict(row)
        item["signers"] = json.loads(item.pop("signers_json"))
        item["claims"] = json.loads(item.pop("claims_json"))
        out.append(item)
    return out


def _conflicts(statements_for_course):
    """Statement ids whose two signers hold opposite opinions."""
    return {
        s["event_id"]
        for s in statements_for_course
        if len({x["decision"] for x in s["signers"]}) > 1
    }


def attest(db, mandate_id):
    """Evaluate one mandate from the current ledgers and retain the result."""
    mandate = db.execute(
        "SELECT * FROM attestation_mandate WHERE mandate_id=?", (mandate_id,)
    ).fetchone()
    if mandate is None:
        raise ValueError(f"unknown mandate: {mandate_id}")
    mandate = dict(mandate)
    course_id = mandate["course_id"]
    old = _load_outline(db, mandate["old_outline_event"])
    new = _load_outline(db, mandate["new_outline_event"])

    pending_reasons = []
    if old is None or new is None:
        pending_reasons.append("referenced_outline_missing")
        old = old or {"objectives": [], "year": None, "event_id": mandate["old_outline_event"]}
        new = new or {"objectives": [], "year": None, "event_id": mandate["new_outline_event"]}

    catalog_state = catalog.current(db, course_id, new["year"])
    if catalog_state["decision"] != "published":
        pending_reasons.append(
            "catalog_stopped" if catalog_state["decision"] == "stopped" else "new_outline_not_eligible"
        )
    release = outline.current(db, course_id, new["year"])
    if release.get("event_id") != mandate["new_outline_event"] or release.get("decision") != "published":
        pending_reasons.append("referenced_outline_superseded")

    all_statements = statements(
        db, course_id, mandate["old_outline_event"], mandate["new_outline_event"]
    )
    conflicting = _conflicts(all_statements)

    scope_mandatory = mandate["kind"] == CONTINUATION
    in_scope = [
        o for o in old["objectives"]
        if (o["mandatory"] if scope_mandatory else True)
    ]
    new_ids = {o["id"] for o in new["objectives"]}

    objective_results = []
    for objective in in_scope:
        required = sorted(required_subrequirements(objective))
        # Only unanimously approved statements count; split signer opinions
        # force the objectives relying on them back to manual review.
        usable = [
            s for s in all_statements
            if s["old_objective_id"] == objective["id"]
            and all(x["decision"] == "approved" for x in s["signers"])
        ]
        affected_conflicts = sorted(
            s["event_id"] for s in all_statements
            if s["old_objective_id"] == objective["id"] and s["event_id"] in conflicting
        )
        covered_subs = set()
        evidence = []
        invalid_evidence = []
        for s in usable:
            for claim in s["claims"]:
                entry = {
                    "succession_event": s["event_id"],
                    "new_objective_id": claim["new_objective_id"],
                    "covered_subrequirements": claim["covered_subrequirements"],
                }
                if claim["new_objective_id"] not in new_ids:
                    invalid_evidence.append(entry)
                    continue
                covered_subs.update(claim["covered_subrequirements"])
                evidence.append(entry)
        missing = [sub for sub in required if sub not in covered_subs]
        if affected_conflicts:
            status = "signoff_conflict"
        elif not usable:
            status = "no_approved_succession"
        elif invalid_evidence and not evidence:
            status = "evidence_not_in_new_outline"
        elif missing:
            status = "partial_coverage"
        else:
            status = "covered"
        objective_results.append(
            {
                "old_objective_id": objective["id"],
                "mandatory": objective["mandatory"],
                "required_subrequirements": required,
                "covered_subrequirements": sorted(covered_subs),
                "missing_subrequirements": missing,
                "evidence": evidence,
                "invalid_evidence": invalid_evidence,
                "conflicting_statements": affected_conflicts,
                "status": status,
            }
        )

    if pending_reasons:
        decision, reason = PENDING_REVIEW, ",".join(pending_reasons)
    elif any(r["status"] == "signoff_conflict" for r in objective_results):
        decision, reason = PENDING_REVIEW, "signoff_conflict"
    elif all(r["status"] == "covered" for r in objective_results):
        decision, reason = APPROVED, "all_outcomes_covered"
    else:
        decision, reason = REJECTED, "coverage_incomplete"

    detail = {
        "mandate_id": mandate_id,
        "course_id": course_id,
        "kind": mandate["kind"],
        "old_outline_event": mandate["old_outline_event"],
        "new_outline_event": mandate["new_outline_event"],
        "new_outline_decision": release.get("decision"),
        "new_outline_release_event": release.get("event_id"),
        "catalog_decision": catalog_state["decision"],
        "scope": "mandatory_only" if scope_mandatory else "all_objectives",
        "conflicting_statements": sorted(conflicting),
        "objectives": objective_results,
    }
    body = json.dumps(
        {"mandate_id": mandate_id, "decision": decision, "reason": reason, "detail": detail},
        ensure_ascii=False,
        sort_keys=True,
    )
    db.execute(
        "INSERT INTO attestation_case(mandate_id,course_id,kind,old_outline_event,"
        "new_outline_event,decision,reason,detail_json) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(mandate_id) DO UPDATE SET course_id=excluded.course_id,"
        "kind=excluded.kind,old_outline_event=excluded.old_outline_event,"
        "new_outline_event=excluded.new_outline_event,decision=excluded.decision,"
        "reason=excluded.reason,detail_json=excluded.detail_json,"
        "updated_at=CURRENT_TIMESTAMP",
        (
            mandate_id,
            course_id,
            mandate["kind"],
            mandate["old_outline_event"],
            mandate["new_outline_event"],
            decision,
            reason,
            json.dumps(detail, ensure_ascii=False, sort_keys=True),
        ),
    )
    # Append-only run history: prior-year attestations and their raw outlines
    # stay traceable even when a later run flips the decision.
    db.execute(
        "INSERT INTO attestation_report(mandate_id,decision,reason,body) VALUES (?,?,?,?)",
        (mandate_id, decision, reason, body),
    )
    return json.loads(body)


def reverify_all(db):
    """Recompute every mandate after outline or catalog release changes."""
    return [
        attest(db, row["mandate_id"])
        for row in rows(db, "SELECT mandate_id FROM attestation_mandate ORDER BY mandate_id")
    ]


def current(db, mandate_id):
    row = db.execute(
        "SELECT * FROM attestation_case WHERE mandate_id=?", (mandate_id,)
    ).fetchone()
    if row is None:
        return {"mandate_id": mandate_id, "decision": "missing"}
    result = dict(row)
    result["detail"] = json.loads(result.pop("detail_json"))
    return result
