"""Independent collection of scored learning-outcome evidence."""
from .sources import record
from .store import rows
from . import outline


def ingest(db, events):
    affected = set()
    count = 0
    with db:
        for event in events:
            if event.get("op") != "assessment":
                raise ValueError("assessment input must be assessment")
            if not 0 <= event["score"] <= 100 or event["rubric_version"] < 1:
                raise ValueError("invalid assessment score or rubric")
            current = outline.current(db, event["course_id"], event["year"])
            if current["decision"] != "published":
                raise ValueError("assessment requires published outline")
            if event["objective_id"] not in {x["id"] for x in current["objectives"]}:
                raise ValueError("unknown learning objective")
            if record(db, event, "assessment", event["student_id"]):
                db.execute(
                    "INSERT INTO assessment_evidence VALUES (?,?,?,?,?,?,?,?)",
                    (
                        event["event_id"], event["student_id"], event["course_id"],
                        event["year"], event["objective_id"], event["rubric_version"],
                        event["score"], event["assessor"],
                    ),
                )
                count += 1
                affected.add(
                    (event["student_id"], event["course_id"], event["year"], event["objective_id"])
                )
        cases = [decide(db, *key) for key in sorted(affected)]
    return {"imported": count, "cases": cases}


def decide(db, student_id, course_id, year, objective_id):
    current = outline.current(db, course_id, year)
    if current["decision"] != "published":
        decision, source = "pending", None
    else:
        items = rows(
            db,
            "SELECT a.*,e.event_time FROM assessment_evidence a JOIN evidence e USING(event_id) "
            "WHERE a.student_id=? AND a.course_id=? AND a.year=? AND a.objective_id=? "
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

