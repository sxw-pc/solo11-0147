"""Independent collection of signed course outlines and learning outcomes."""
import json
from .sources import record
from .store import rows
from . import catalog


def ingest(db, events):
    count = 0
    with db:
        for event in events:
            if event.get("op") != "outline":
                raise ValueError("outline input must be outline")
            objectives = event["objectives"]
            if not objectives or len({x["id"] for x in objectives}) != len(objectives):
                raise ValueError("outline requires distinct objectives")
            for objective in objectives:
                if not objective["id"] or not objective["text"].strip():
                    raise ValueError("outline objective is empty")
                if not isinstance(objective["mandatory"], bool):
                    raise ValueError("mandatory must be boolean")
            if record(db, event, "outline", event["course_id"]):
                db.execute(
                    "INSERT INTO outline_revision VALUES (?,?,?,?,?,?)",
                    (
                        event["event_id"],
                        event["course_id"],
                        event["year"],
                        event["version"],
                        event["issuer"],
                        json.dumps(objectives, ensure_ascii=False, sort_keys=True),
                    ),
                )
                count += 1
        for key in rows(db, "SELECT DISTINCT course_id,year FROM outline_revision"):
            release(db, key["course_id"], key["year"])
    return {"imported": count, "outlines": rows(db, "SELECT * FROM outline_release")}


def release(db, course_id, year):
    course = catalog.current(db, course_id, year)
    department = None
    if course["decision"] == "published":
        row = db.execute(
            "SELECT department FROM course_revision WHERE event_id=?",
            (course["event_id"],),
        ).fetchone()
        department = row["department"] if row else None
    items = rows(
        db,
        "SELECT * FROM outline_revision WHERE course_id=? AND year=? ORDER BY version DESC,event_id",
        (course_id, year),
    )
    trusted = [item for item in items if item["issuer"] == department]
    if not trusted:
        chosen = None
        decision, reason = "pending", "no_authorized_outline"
    else:
        top = [item for item in trusted if item["version"] == trusted[0]["version"]]
        if len({item["objectives_json"] for item in top}) > 1:
            chosen = None
            decision, reason = "disputed", "same_version_content_conflict"
        else:
            chosen = top[0]["event_id"]
            decision, reason = "published", "authorized_outline"
    db.execute(
        "INSERT INTO outline_release VALUES (?,?,?,?,?) ON CONFLICT(course_id,year) "
        "DO UPDATE SET event_id=excluded.event_id,decision=excluded.decision,reason=excluded.reason",
        (course_id, year, chosen, decision, reason),
    )
    if decision == "published":
        # An outline version change only re-opens the marking batches of this
        # course/year. Sign-offs recorded against earlier outline versions stay
        # in batch_review_history untouched, so every old decision is traceable.
        from . import assessment

        objective_ids = {x["id"] for x in current(db, course_id, year)["objectives"]}
        for row in rows(
            db,
            "SELECT DISTINCT objective_id FROM assessment_evidence WHERE course_id=? AND year=?",
            (course_id, year),
        ):
            if row["objective_id"] in objective_ids:
                assessment._reevaluate_if_signed(db, course_id, year, row["objective_id"])
    return current(db, course_id, year)


def current(db, course_id, year):
    row = db.execute(
        "SELECT * FROM outline_release WHERE course_id=? AND year=?",
        (course_id, year),
    ).fetchone()
    if not row:
        return {"course_id": course_id, "year": year, "decision": "missing"}
    result = dict(row)
    if row["event_id"]:
        item = db.execute(
            "SELECT objectives_json,version FROM outline_revision WHERE event_id=?",
            (row["event_id"],),
        ).fetchone()
        result["objectives"] = json.loads(item["objectives_json"])
        result["version"] = item["version"]
    return result

