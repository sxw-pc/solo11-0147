"""Independent offline course-evaluation form and response intake."""
from .sources import record
from .store import rows
from . import catalog


def current_form(db, course_id, year, form_id):
    if catalog.current(db, course_id, year)["decision"] != "published":
        return {"decision": "pending_catalog"}
    items = rows(
        db,
        "SELECT * FROM survey_form WHERE course_id=? AND year=? AND form_id=? "
        "ORDER BY version DESC,event_id",
        (course_id, year, form_id),
    )
    if not items:
        return {"decision": "missing"}
    top = [item for item in items if item["version"] == items[0]["version"]]
    if len({item["question"] for item in top}) > 1:
        return {"decision": "disputed"}
    return {"decision": "published", "event_id": top[0]["event_id"], "version": top[0]["version"]}


def ingest(db, data):
    affected = set()
    count = 0
    with db:
        for event in data.get("forms", []):
            if event["version"] < 1 or not event["question"].strip():
                raise ValueError("survey form needs version and question")
            if record(db, event, "survey-form", event["form_id"]):
                db.execute(
                    "INSERT INTO survey_form VALUES (?,?,?,?,?,?,?)",
                    (event["event_id"], event["course_id"], event["year"],
                     event["form_id"], event["version"], event["question"], event["issuer"]),
                )
                count += 1
                affected.add((event["course_id"], event["year"], event["form_id"]))
        for event in data.get("responses", []):
            key = (event["course_id"], event["year"], event["form_id"])
            if current_form(db, *key)["decision"] != "published":
                raise ValueError("response requires published form")
            if not 1 <= event["score"] <= 5:
                raise ValueError("score outside 1..5")
            if record(db, event, "survey-response", event["response_token"]):
                db.execute(
                    "INSERT INTO survey_response VALUES (?,?,?,?,?,?)",
                    (event["event_id"], *key, event["response_token"], event["score"]),
                )
                count += 1
                affected.add(key)
        cases = [decide(db, *key) for key in sorted(affected)]
    return {"imported": count, "cases": cases}


def decide(db, course_id, year, form_id):
    form = current_form(db, course_id, year, form_id)
    records = rows(
        db,
        "SELECT r.*,e.event_time FROM survey_response r JOIN evidence e USING(event_id) "
        "WHERE r.course_id=? AND r.year=? AND r.form_id=? "
        "ORDER BY e.event_time DESC,r.event_id DESC",
        (course_id, year, form_id),
    )
    latest = {}
    for item in records:
        latest.setdefault(item["response_token"], item)
    scores = [item["score"] for item in latest.values()]
    mean = sum(scores) / len(scores) if scores else None
    decision = "reported" if form["decision"] == "published" and scores else "pending"
    db.execute(
        "INSERT INTO survey_case VALUES (?,?,?,?,?,?) ON CONFLICT(course_id,year,form_id) "
        "DO UPDATE SET responses=excluded.responses,mean_score=excluded.mean_score,decision=excluded.decision",
        (course_id, year, form_id, len(scores), mean, decision),
    )
    return {"course_id": course_id, "year": year, "form_id": form_id,
            "responses": len(scores), "mean_score": mean, "decision": decision}

