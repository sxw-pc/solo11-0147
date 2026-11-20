"""Independent collection of scored learning-outcome evidence.

Personal ``decide`` reports keep their original semantics: they judge whether a
single student attained an objective. Batch-level quality review
(``review_batch``) is a separate sign-off over the whole marking group and
never treats a student's ``attained`` result as proof of marking quality.
"""
import json
from collections import defaultdict, deque
from datetime import datetime, timezone

from .sources import record
from .store import rows
from . import outline

VALID_STATUS = ("active", "revoked", "duplicate")


def ingest(db, events):
    affected = set()
    count = 0
    with db:
        for event in events:
            if event.get("op") != "assessment":
                raise ValueError("assessment input must be assessment")
            if not 0 <= event["score"] <= 100 or event["rubric_version"] < 1:
                raise ValueError("invalid assessment score or rubric")
            status = event.get("status", "active")
            if status not in VALID_STATUS:
                raise ValueError("unknown assessment status")
            current = outline.current(db, event["course_id"], event["year"])
            if current["decision"] != "published":
                raise ValueError("assessment requires published outline")
            if event["objective_id"] not in {x["id"] for x in current["objectives"]}:
                raise ValueError("unknown learning objective")
            paper_id = event.get("paper_id")
            if record(db, event, "assessment", event["student_id"]):
                stored_status = status
                if status == "revoked":
                    # A review withdrawal deactivates the previous submission of
                    # the same answer sheet (or, for legacy paperless evidence,
                    # the same student/objective) while keeping both raw rows.
                    if paper_id is not None:
                        db.execute(
                            "UPDATE assessment_evidence SET status='revoked' "
                            "WHERE course_id=? AND year=? AND objective_id=? "
                            "AND paper_id=? AND assessor=? AND rubric_version=? AND status='active'",
                            (
                                event["course_id"], event["year"], event["objective_id"],
                                paper_id, event["assessor"], event["rubric_version"],
                            ),
                        )
                    else:
                        db.execute(
                            "UPDATE assessment_evidence SET status='revoked' "
                            "WHERE course_id=? AND year=? AND objective_id=? "
                            "AND student_id=? AND assessor=? AND rubric_version=? AND status='active'",
                            (
                                event["course_id"], event["year"], event["objective_id"],
                                event["student_id"], event["assessor"], event["rubric_version"],
                            ),
                        )
                elif paper_id is not None:
                    # A resubmission of the same answer sheet by the same marker
                    # under the same rubric is retained as raw evidence but must
                    # not enter quality statistics as a second independent score.
                    clash = db.execute(
                        "SELECT 1 FROM assessment_evidence WHERE course_id=? AND year=? "
                        "AND objective_id=? AND paper_id=? AND assessor=? AND rubric_version=? "
                        "AND status='active' LIMIT 1",
                        (
                            event["course_id"], event["year"], event["objective_id"],
                            paper_id, event["assessor"], event["rubric_version"],
                        ),
                    ).fetchone()
                    if clash:
                        stored_status = "duplicate"
                db.execute(
                    "INSERT INTO assessment_evidence VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        event["event_id"], event["student_id"], event["course_id"],
                        event["year"], event["objective_id"], event["rubric_version"],
                        event["score"], event["assessor"], paper_id, stored_status,
                    ),
                )
                count += 1
                affected.add(
                    (event["course_id"], event["year"], event["objective_id"])
                )
        cases = []
        for course_id, year, objective_id in sorted(affected):
            students = rows(
                db,
                "SELECT DISTINCT student_id FROM assessment_evidence "
                "WHERE course_id=? AND year=? AND objective_id=?",
                (course_id, year, objective_id),
            )
            for row in students:
                cases.append(
                    decide(db, row["student_id"], course_id, year, objective_id)
                )
        reviews = [
            review for key in sorted(affected)
            if (review := _reevaluate_if_signed(db, *key)) is not None
        ]
    return {"imported": count, "cases": cases, "reviews": reviews}


def calibrate(db, events):
    """Capture official anchor values used as the external certification ruler."""
    count = 0
    affected = set()
    with db:
        for event in events:
            if event.get("op") != "anchor_calibration":
                raise ValueError("calibration input must be anchor_calibration")
            if not 0 <= event["official_score"] <= 100 or event["rubric_version"] < 1:
                raise ValueError("invalid calibration score or rubric")
            if not event.get("paper_id"):
                raise ValueError("calibration requires anchor paper identity")
            current = outline.current(db, event["course_id"], event["year"])
            if current["decision"] != "published":
                raise ValueError("calibration requires published outline")
            if event["objective_id"] not in {x["id"] for x in current["objectives"]}:
                raise ValueError("unknown learning objective")
            if record(db, event, "anchor_calibration", event["paper_id"]):
                db.execute(
                    "INSERT INTO anchor_calibration VALUES (?,?,?,?,?,?,?,?)",
                    (
                        event["event_id"], event["course_id"], event["year"],
                        event["objective_id"], event["paper_id"],
                        event["rubric_version"], event["official_score"], event["issuer"],
                    ),
                )
                count += 1
                affected.add(
                    (event["course_id"], event["year"], event["objective_id"])
                )
        reviews = [
            review for key in sorted(affected)
            if (review := _reevaluate_if_signed(db, *key)) is not None
        ]
    return {"imported": count, "reviews": reviews}


def decide(db, student_id, course_id, year, objective_id):
    """Personal learning-outcome decision; unchanged semantics."""
    current = outline.current(db, course_id, year)
    if current["decision"] != "published":
        decision, source = "pending", None
    else:
        items = rows(
            db,
            "SELECT a.*,e.event_time FROM assessment_evidence a JOIN evidence e USING(event_id) "
            "WHERE a.student_id=? AND a.course_id=? AND a.year=? AND a.objective_id=? "
            "AND a.status='active' "
            "ORDER BY e.event_time DESC,a.event_id DESC",
            (student_id, course_id, year, objective_id),
        )
        source = items[0]["event_id"] if items else None
        decision = "missing" if not items else "attained" if items[0]["score"] >= 70 else "not_attained"
    db.execute(
        "INSERT INTO assessment_case VALUES (?,?,?,?,?,?) ON CONFLICT(student_id,course_id,year,objective_id) "
        "DO UPDATE SET decision=excluded.decision,source_event=excluded.source_event",
        (student_id, course_id, year, objective_id, decision, source),
    )
    return {
        "student_id": student_id, "course_id": course_id, "year": year,
        "objective_id": objective_id, "decision": decision, "source_event": source,
    }


def _active_evidence(db, course_id, year, objective_id):
    return rows(
        db,
        "SELECT a.*,e.event_time FROM assessment_evidence a JOIN evidence e USING(event_id) "
        "WHERE a.course_id=? AND a.year=? AND a.objective_id=? AND a.status='active' "
        "ORDER BY e.event_time,a.event_id",
        (course_id, year, objective_id),
    )


def _calibrations(db, course_id, year, objective_id):
    latest = {}
    for item in rows(
        db,
        "SELECT c.*,e.event_time FROM anchor_calibration c JOIN evidence e USING(event_id) "
        "WHERE c.course_id=? AND c.year=? AND c.objective_id=? "
        "ORDER BY e.event_time,c.event_id",
        (course_id, year, objective_id),
    ):
        latest[(item["paper_id"], item["rubric_version"])] = item
    return latest


def _build_graph(evidence, calibrations):
    """Graph of (assessor, rubric_version) nodes joined by shared anchor papers.

    Rubric version is part of the node identity: two markers using different
    rubric versions on the same paper are never comparable, so a mid-batch
    rubric switch splits the component instead of mixing the two scales.
    """
    nodes = set()
    by_paper = defaultdict(list)
    for item in evidence:
        node = (item["assessor"], item["rubric_version"])
        nodes.add(node)
        if item["paper_id"] is not None:
            by_paper[(item["paper_id"], item["rubric_version"])].append(item)

    edges = {}
    for (paper_id, rubric_version), items in sorted(by_paper.items()):
        official = calibrations.get((paper_id, rubric_version))
        for left in sorted(items, key=lambda r: r["assessor"]):
            for right in sorted(items, key=lambda r: r["assessor"]):
                if left["assessor"] >= right["assessor"]:
                    continue
                a_node = (left["assessor"], rubric_version)
                b_node = (right["assessor"], rubric_version)
                key = tuple(sorted((a_node, b_node)))
                bucket = edges.setdefault(key, [])
                bucket.append(
                    {
                        "paper_id": paper_id,
                        "rubric_version": rubric_version,
                        "assessor_a": left["assessor"],
                        "assessor_b": right["assessor"],
                        "score_a": left["score"],
                        "score_b": right["score"],
                        "difference": abs(left["score"] - right["score"]),
                        "calibrated": official is not None,
                    }
                )
    return nodes, edges


def _components(nodes, edges):
    adjacency = defaultdict(set)
    for a_node, b_node in edges:
        adjacency[a_node].add(b_node)
        adjacency[b_node].add(a_node)
    seen = set()
    groups = []
    for node in sorted(nodes):
        if node in seen:
            continue
        queue = deque([node])
        group = []
        seen.add(node)
        while queue:
            current = queue.popleft()
            group.append(current)
            for nxt in sorted(adjacency[current]):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        groups.append(sorted(group))
    return groups


def _estimate_bias(group, evidence, edges, calibrations):
    """Per-marker bias against the official ruler, propagated across anchors.

    Bias means marker score minus official score. Markers with a calibrated
    anchor are direct roots; every other marker inherits the offset along
    shared-anchor edges. Difficulty belongs to the paper and cancels out of
    pairwise differences, so marker bias is never confused with paper difficulty.
    """
    group_set = set(group)
    roots = {}
    for item in evidence:
        if (item["assessor"], item["rubric_version"]) not in group_set:
            continue
        if item["paper_id"] is None:
            continue
        official = calibrations.get((item["paper_id"], item["rubric_version"]))
        if official is None:
            continue
        roots.setdefault((item["assessor"], item["rubric_version"]), []).append(
            item["score"] - official["official_score"]
        )
    root_bias = {node: sum(values) / len(values) for node, values in roots.items()}

    estimates = {node: [] for node in group}
    adjacency = defaultdict(dict)
    for (a_node, b_node), details in edges.items():
        if a_node not in group_set:
            continue
        # score_a - score_b = bias(a) - bias(b) because paper difficulty cancels,
        # so moving from node a to node b adds (score_b - score_a) to the bias.
        signed = sum(d["score_b"] - d["score_a"] for d in details) / len(details)
        adjacency[a_node][b_node] = signed
        adjacency[b_node][a_node] = -signed

    for root, root_value in sorted(root_bias.items()):
        queue = deque([(root, root_value)])
        reached = {root: root_value}
        while queue:
            node, value = queue.popleft()
            estimates[node].append(value)
            for nxt, offset in sorted(adjacency[node].items()):
                if nxt not in group_set or nxt in reached:
                    continue
                reached[nxt] = value + offset
                queue.append((nxt, value + offset))

    bias = {}
    for node in group:
        if estimates[node]:
            bias[node] = sum(estimates[node]) / len(estimates[node])
    return root_bias, bias


def review_batch(db, course_id, year, objective_id, tolerance, save=True):
    """Issue both quality sign-offs for one marking batch in one evidence report."""
    if tolerance < 0:
        raise ValueError("tolerance must be non-negative")
    current = outline.current(db, course_id, year)
    if current["decision"] != "published" or not current.get("event_id"):
        raise ValueError("batch review requires published outline")
    objective_ids = {x["id"] for x in current["objectives"]}
    if objective_id not in objective_ids:
        raise ValueError("unknown learning objective")

    evidence = _active_evidence(db, course_id, year, objective_id)
    calibrations = _calibrations(db, course_id, year, objective_id)
    nodes, edges = _build_graph(evidence, calibrations)
    groups = _components(nodes, edges)

    violations = [
        detail
        for details in edges.values()
        for detail in details
        if detail["difference"] > tolerance
    ]
    isolated = [
        {"assessor": node[0], "rubric_version": node[1]}
        for group in groups
        for node in group
        if len(group) == 1
    ]

    internal_reason = (
        "no_marking_evidence" if not nodes
        else "pairwise_tolerance_exceeded" if violations
        else "within_tolerance"
    )
    internal_decision = (
        "pending_evidence" if not nodes
        else "withheld" if violations
        else "publishable"
    )

    component_reports = []
    external_reasons = set()
    if internal_decision != "publishable":
        external_decision = "withheld"
        external_reasons.add("internal_review_failed")
    else:
        for group in groups:
            root_bias, bias = _estimate_bias(group, evidence, edges, calibrations)
            calibrated = sorted(root_bias)
            over_tolerance = sorted(
                node for node, value in bias.items() if abs(value) > tolerance
            )
            uncalibrated_nodes = sorted(node for node in group if node not in bias)
            if not calibrated or uncalibrated_nodes:
                status = "uncalibrated"
                external_reasons.add("component_without_official_anchor")
            elif over_tolerance:
                status = "bias_out_of_tolerance"
                external_reasons.add("systematic_bias_out_of_tolerance")
            else:
                status = "calibrated"
            component_reports.append(
                {
                    "nodes": [
                        {"assessor": a, "rubric_version": rv} for a, rv in group
                    ],
                    "status": status,
                    "calibrated_nodes": [
                        {"assessor": a, "rubric_version": rv} for a, rv in calibrated
                    ],
                    "bias": [
                        {
                            "assessor": a,
                            "rubric_version": rv,
                            "bias": round(bias[(a, rv)], 4),
                            "directly_calibrated": (a, rv) in root_bias,
                        }
                        for a, rv in group
                        if (a, rv) in bias
                    ],
                    "over_tolerance": [
                        {"assessor": a, "rubric_version": rv} for a, rv in over_tolerance
                    ],
                    "uncalibrated_nodes": [
                        {"assessor": a, "rubric_version": rv}
                        for a, rv in uncalibrated_nodes
                    ],
                }
            )
        if not nodes:
            external_decision = "pending_evidence"
            external_reasons.add("no_marking_evidence")
        elif external_reasons:
            external_decision = "withheld"
        else:
            external_decision = "certified"
            external_reasons.add("within_official_tolerance")

    anchor_papers = {}
    for item in evidence:
        if item["paper_id"] is None:
            continue
        official = calibrations.get((item["paper_id"], item["rubric_version"]))
        anchor_papers.setdefault(
            (item["paper_id"], item["rubric_version"]),
            {
                "paper_id": item["paper_id"],
                "rubric_version": item["rubric_version"],
                "official_score": official["official_score"] if official else None,
                "calibration_event": official["event_id"] if official else None,
                "scores": [],
            },
        )["scores"].append(
            {
                "assessor": item["assessor"],
                "score": item["score"],
                "event_id": item["event_id"],
            }
        )

    duplicate_rows = rows(
        db,
        "SELECT a.event_id,a.paper_id,a.assessor,a.rubric_version,a.score,e.event_time "
        "FROM assessment_evidence a JOIN evidence e USING(event_id) "
        "WHERE a.course_id=? AND a.year=? AND a.objective_id=? AND a.status='duplicate' "
        "ORDER BY e.event_time,a.event_id",
        (course_id, year, objective_id),
    )
    revoked_rows = rows(
        db,
        "SELECT a.event_id,a.paper_id,a.assessor,a.rubric_version,a.score,e.event_time "
        "FROM assessment_evidence a JOIN evidence e USING(event_id) "
        "WHERE a.course_id=? AND a.year=? AND a.objective_id=? AND a.status='revoked' "
        "ORDER BY e.event_time,a.event_id",
        (course_id, year, objective_id),
    )

    report = {
        "batch": {
            "course_id": course_id,
            "year": year,
            "objective_id": objective_id,
            "tolerance": tolerance,
        },
        "outline": {
            "release_event": current["event_id"],
            "version": current["version"],
        },
        "internal_review": {
            "decision": internal_decision,
            "reason": internal_reason,
        },
        "external_certification": {
            "decision": external_decision,
            "reasons": sorted(external_reasons),
        },
        "components": component_reports,
        "anchor_papers": [anchor_papers[key] for key in sorted(anchor_papers)],
        "pairwise_checks": [
            detail
            for key in sorted(edges)
            for detail in edges[key]
        ],
        "violations": violations,
        "isolated_nodes": isolated,
        "excluded_evidence": {
            "duplicates": [dict(r) for r in duplicate_rows],
            "revoked": [dict(r) for r in revoked_rows],
        },
        "raw_score_events": sorted(
            {item["event_id"] for item in evidence}
            | {r["event_id"] for r in duplicate_rows}
            | {r["event_id"] for r in revoked_rows}
        ),
    }

    created_at = datetime.now(timezone.utc).isoformat()
    if save:
        db.execute(
            "INSERT INTO batch_review_history "
            "(course_id,year,objective_id,outline_version,tolerance,internal_decision,"
            "external_decision,reason_json,body,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                course_id, year, objective_id, current["version"], tolerance,
                internal_decision, external_decision,
                json.dumps(
                    {
                        "internal": internal_reason,
                        "external": sorted(external_reasons),
                    },
                    sort_keys=True,
                ),
                json.dumps(report, ensure_ascii=False, sort_keys=True),
                created_at,
            ),
        )
        review_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        report["review_id"] = review_id
    report["created_at"] = created_at
    return report


def _reevaluate_if_signed(db, course_id, year, objective_id):
    """Re-run only batches already signed, so old sign-offs stay reproducible."""
    last = db.execute(
        "SELECT tolerance FROM batch_review_history WHERE course_id=? AND year=? "
        "AND objective_id=? ORDER BY id DESC LIMIT 1",
        (course_id, year, objective_id),
    ).fetchone()
    if last is None:
        return None
    return review_batch(db, course_id, year, objective_id, last["tolerance"])


def review_history(db, course_id, year, objective_id):
    return rows(
        db,
        "SELECT id,course_id,year,objective_id,outline_version,tolerance,"
        "internal_decision,external_decision,reason_json,created_at "
        "FROM batch_review_history WHERE course_id=? AND year=? AND objective_id=? "
        "ORDER BY id",
        (course_id, year, objective_id),
    )
