"""Independent offline course-evaluation form and response intake.

The ledger has two faces:

* :func:`decide` keeps the internal report (response count and exact mean) for
  the academic affairs office.  It is never removed or rewritten.
* :func:`publish` releases *aggregates* only: a count, a total and a rounded
  mean for a named group of response tokens.  Every application is versioned
  and stored append-only.  Before an application is released it is checked
  against every aggregate that was actually published before it (see
  :mod:`curriculum.inference`).  A release that would pin any participant's
  original 1..5 score down is frozen and kept as evidence, including the
  internal tokens, the public versions responsible and a reviewable constraint
  summary.  Those internals never appear in :func:`public_report`.

Catalog releases are a publication gate: if the year's course is stopped,
disputed or otherwise unpublished, later survey publications for that
course/year are frozen with catalog evidence.  Restoring the catalog allows a
new application, but never rewrites history.
"""
import json

from . import catalog, inference
from .sources import record
from .store import rows

DEFAULT_PRECISION = 2


# ---------------------------------------------------------------------------
# Form and response state
# ---------------------------------------------------------------------------

def _form_state(db, course_id, year, form_id):
    """Form decision without consulting the catalog ledger."""
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
        return {"decision": "disputed", "version": top[0]["version"]}
    return {"decision": "published", "event_id": top[0]["event_id"],
            "version": top[0]["version"]}


def current_form(db, course_id, year, form_id):
    if catalog.current(db, course_id, year)["decision"] != "published":
        return {"decision": "pending_catalog"}
    return _form_state(db, course_id, year, form_id)


def _latest_responses(db, course_id, year, form_id, form_version):
    """Latest effective event per token, limited to the current form version."""
    records = rows(
        db,
        "SELECT r.*,e.event_time FROM survey_response r JOIN evidence e USING(event_id) "
        "WHERE r.course_id=? AND r.year=? AND r.form_id=? "
        "AND (r.form_version IS NULL OR r.form_version=?) "
        "ORDER BY e.event_time DESC,r.event_id DESC",
        (course_id, year, form_id, form_version),
    )
    latest = {}
    for item in records:
        latest.setdefault(item["response_token"], item)
    return latest


def _catalog_snapshot(db, course_id, year):
    state = catalog.current(db, course_id, year)
    return {key: state.get(key) for key in
            ("course_id", "year", "decision", "reason", "event_id")}


def _record_catalog_evidence(db, course_id, year, snapshot):
    db.execute(
        "INSERT INTO survey_catalog_evidence"
        "(course_id,year,catalog_decision,catalog_reason,catalog_event_id,source) "
        "VALUES (?,?,?,?,?,?)",
        (course_id, year, snapshot["decision"], snapshot["reason"],
         snapshot["event_id"], "survey-publish"),
    )


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------

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
                     event["form_id"], event["version"], event["question"],
                     event["issuer"]),
                )
                count += 1
                affected.add((event["course_id"], event["year"], event["form_id"]))
        for event in data.get("responses", []):
            key = (event["course_id"], event["year"], event["form_id"])
            form = current_form(db, *key)
            if form["decision"] != "published":
                raise ValueError("response requires published form")
            if not 1 <= event["score"] <= 5:
                raise ValueError("score outside 1..5")
            if record(db, event, "survey-response", event["response_token"]):
                db.execute(
                    "INSERT INTO survey_response VALUES (?,?,?,?,?,?,?)",
                    (event["event_id"], *key, event["response_token"],
                     event["score"], form["version"]),
                )
                count += 1
                affected.add(key)
        cases = [decide(db, *key) for key in sorted(affected)]
    return {"imported": count, "cases": cases}


# ---------------------------------------------------------------------------
# Internal report (counts and exact mean; never removed or rewritten)
# ---------------------------------------------------------------------------

def decide(db, course_id, year, form_id, save=True):
    form = _form_state(db, course_id, year, form_id)
    snapshot = _catalog_snapshot(db, course_id, year)
    # The internal report keeps counts and the exact mean even while the
    # public release is blocked by a form dispute or a catalog decision.
    latest = _latest_responses(db, course_id, year, form_id, form.get("version")) \
        if form.get("version") is not None else {}
    scores = [item["score"] for item in latest.values()]
    mean = sum(scores) / len(scores) if scores else None
    decision = "reported" if form["decision"] == "published" and scores else "pending"
    if save:
        db.execute(
            "INSERT INTO survey_case VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(course_id,year,form_id) DO UPDATE SET "
            "responses=excluded.responses,mean_score=excluded.mean_score,"
            "decision=excluded.decision,form_version=excluded.form_version,"
            "form_decision=excluded.form_decision,catalog_decision=excluded.catalog_decision",
            (course_id, year, form_id, len(scores), mean, decision,
             form.get("version"), form["decision"], snapshot["decision"]),
        )
    return {"course_id": course_id, "year": year, "form_id": form_id,
            "responses": len(scores), "mean_score": mean, "decision": decision,
            "form_decision": form["decision"], "form_version": form.get("version"),
            "catalog_decision": snapshot["decision"]}


# ---------------------------------------------------------------------------
# Public aggregate publication
# ---------------------------------------------------------------------------

def _normalize_groups(groups):
    normalized = []
    for group in groups:
        tokens = group.get("tokens") if isinstance(group, dict) else group
        if isinstance(tokens, str):
            tokens = tokens.split(",")
        tokens = [str(token).strip() for token in tokens if str(token).strip()]
        if not tokens:
            raise ValueError("public group needs at least one response token")
        normalized.append(list(dict.fromkeys(tokens)))
    if not normalized:
        raise ValueError("publication needs at least one group")
    return normalized


def _published_rows(db, course_id, year, form_id):
    publications = rows(
        db,
        "SELECT * FROM survey_publication WHERE course_id=? AND year=? AND form_id=? "
        "ORDER BY publication_id",
        (course_id, year, form_id),
    )
    for publication in publications:
        publication["members"] = rows(
            db,
            "SELECT response_token,score FROM survey_publication_member "
            "WHERE publication_id=? ORDER BY response_token",
            (publication["publication_id"],),
        )
        publication["tokens"] = [m["response_token"] for m in publication["members"]]
        publication["detail"] = json.loads(publication["detail_json"])
    return publications


def _public_entry(publication, sequence):
    """Fields safe to show publicly: no tokens, no version, no group index."""
    return {
        "sequence": sequence,
        "course_id": publication["course_id"],
        "year": publication["year"],
        "form_id": publication["form_id"],
        "responses": publication["responses"],
        "total_score": publication["total_score"],
        "mean_score": publication["mean_score"],
        "precision": publication["precision"],
        "released_at": publication["created_at"],
    }


def _constraint(publication):
    return {
        "version": publication["version"],
        "kind": "published",
        "tokens": publication["tokens"],
        "total": publication["total_score"],
        "mean": publication["mean_score"],
        "precision": publication["precision"],
    }


def _current_scores(db, course_id, year, form_id, form_version):
    if form_version is None:
        return {}
    return {token: item["score"] for token, item in
            _latest_responses(db, course_id, year, form_id, form_version).items()}


def _active_constraints(publications, current):
    """Published aggregates that still describe the current effective answers.

    A correction replaces a token's score.  The old aggregate remains a public
    fact in history, but it no longer equals the current answers; including it
    would invent a false equality.  Aggregates whose members are all unchanged
    stay live constraints.  Stale versions are reported separately so the
    audit shows history was not silently rewritten.
    """
    active, stale = [], []
    for publication in publications:
        if publication["decision"] != "published":
            continue
        changed = publication["members"] and any(
            member["score"] is not None
            and current.get(member["response_token"]) != member["score"]
            for member in publication["members"]
        )
        if changed:
            stale.append(publication["version"])
        else:
            active.append(_constraint(publication))
    return active, stale


def _draft(version, tokens, total, mean, precision, publish_total):
    return {
        "version": version,
        "kind": "draft",
        "tokens": list(tokens),
        "total": total if publish_total else None,
        "mean": None if publish_total else float(mean),
        "precision": precision,
    }


def _insert_publication(db, key, version, group_index, form, snapshot, group,
                        total, mean, precision, decision, reason, detail,
                        latest, publish_total=True):
    cursor = db.execute(
        "INSERT INTO survey_publication"
        "(course_id,year,form_id,version,group_index,form_version,responses,"
        "total_score,mean_score,precision,decision,reason,catalog_decision,"
        "catalog_reason,catalog_event_id,detail_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (*key, version, group_index, form.get("version"), len(group),
         total if publish_total or decision != "published" else None,
         mean, precision, decision, reason, snapshot["decision"],
         snapshot["reason"], snapshot["event_id"],
         json.dumps(detail, ensure_ascii=False, sort_keys=True)),
    )
    publication_id = cursor.lastrowid
    for token in group:
        db.execute(
            "INSERT INTO survey_publication_member(publication_id,response_token,score) "
            "VALUES (?,?,?)",
            (publication_id, token, (latest or {}).get(token, {}).get("score")),
        )
    return publication_id


def _freeze(db, key, version, form, snapshot, groups, reason, detail, totals=None,
            latest=None, precision=DEFAULT_PRECISION):
    detail = dict(detail, decision=reason, catalog=snapshot,
                  form_version=form.get("version"), requested_version=version)
    for group_index, group in enumerate(groups):
        total, mean = totals[group_index] if totals else (None, None)
        _insert_publication(db, key, version, group_index, form, snapshot, group,
                            total, mean, precision, "frozen", reason,
                            detail, latest, publish_total=True)
    return detail


def _application(db, key, version, decision, reason, form, snapshot, detail):
    publications = _published_rows(db, *key)
    published = [row for row in publications if row["decision"] == "published"]
    entries = [_public_entry(row, index + 1) for index, row in enumerate(published)]
    current = [entry for entry, row in zip(entries, published)
               if row["version"] == version and decision == "published"]
    return {
        "course_id": key[0], "year": key[1], "form_id": key[2],
        "version": version, "decision": decision, "reason": reason,
        "form_version": form.get("version"), "form_decision": form["decision"],
        "catalog": snapshot,
        "public": current,
        "affected_tokens": detail.get("affected_tokens", []),
        "causing_versions": detail.get("causing_versions", []),
        "stale_versions": detail.get("stale_versions", []),
        "constraints": detail.get("constraints", []),
        "domains": detail.get("domains", {}),
    }


def publish(db, course_id, year, form_id, groups, version=None,
            precision=DEFAULT_PRECISION, publish_total=True):
    """Apply for public release of one or more token groups.

    The returned dictionary is the internal application record: it may name
    the affected tokens and the public versions that caused an inference.
    Use :func:`public_report` for the sanitized view.
    """
    if not 0 <= precision <= 4:
        raise ValueError("precision must be between 0 and 4")
    groups = _normalize_groups(groups)
    key = (course_id, year, form_id)
    with db:
        form = _form_state(db, course_id, year, form_id)
        snapshot = _catalog_snapshot(db, course_id, year)
        history = _published_rows(db, *key)
        versions = {row["version"] for row in history}
        if version is None:
            version = (max(versions) + 1) if versions else 1
        else:
            version = int(version)
            if version in versions:
                raise ValueError(f"publication version already exists: {version}")

        # A cross-ledger gate: the catalog decision for this course/year is
        # checked at application time, and a freeze leaves catalog evidence.
        if snapshot["decision"] != "published":
            _record_catalog_evidence(db, course_id, year, snapshot)
            reason = f"catalog_{snapshot['decision']}"
            detail = _freeze(db, key, version, form, snapshot, groups, reason,
                             {"catalog": snapshot, "affected_tokens": []})
            return _application(db, key, version, "frozen", reason, form,
                                snapshot, detail)

        if form["decision"] != "published":
            reason = f"form_{form['decision']}"
            detail = _freeze(db, key, version, form, snapshot, groups, reason,
                             {"form_decision": form["decision"],
                              "affected_tokens": []})
            return _application(db, key, version, "frozen", reason, form,
                                snapshot, detail)

        current = _current_scores(db, course_id, year, form_id, form["version"])
        latest = _latest_responses(db, course_id, year, form_id, form["version"])
        missing = sorted({token for group in groups for token in group
                          if token not in latest})
        if missing:
            reason = "invalid_token"
            detail = _freeze(db, key, version, form, snapshot, groups, reason,
                             {"affected_tokens": missing, "missing": missing,
                              "causing_versions": []})
            return _application(db, key, version, "frozen", reason, form,
                                snapshot, detail)

        totals = []
        for group in groups:
            total = sum(latest[token]["score"] for token in group)
            mean = inference.round_mean(total, len(group), precision)
            totals.append((total, float(mean)))

        active, stale_versions = _active_constraints(history, current)
        drafts = [_draft(version, group, total, mean, precision, publish_total)
                  for group, (total, mean) in zip(groups, totals)]
        # Every token that appears in a published or proposed aggregate is
        # checked: an inference can pin a person outside the newest group.
        # Fresh tokens have no history and remain unconstrained.
        checked = sorted({token for c in active + drafts for token in c["tokens"]})
        try:
            before = inference.analyze(checked, active)
            after = inference.analyze(checked, active + drafts)
        except inference.InferenceUnverifiable:
            detail = _freeze(db, key, version, form, snapshot, groups,
                             "inference_unverifiable",
                             {"affected_tokens": [], "causing_versions": [],
                              "stale_versions": stale_versions,
                              "constraints": inference.summary(active + drafts)},
                             totals=totals, latest=latest)
            return _application(db, key, version, "frozen",
                                "inference_unverifiable", form, snapshot, detail)
        except ValueError:
            detail = _freeze(db, key, version, form, snapshot, groups,
                             "history_conflict",
                             {"affected_tokens": [], "causing_versions": [],
                              "stale_versions": stale_versions,
                              "constraints": inference.summary(active)},
                             totals=totals, latest=latest)
            return _application(db, key, version, "frozen", "history_conflict",
                                form, snapshot, detail)

        # Report every person whose score becomes uniquely determined once the
        # new aggregate is combined with history; this can include people who
        # are not members of the new group (differential disclosure).
        forced_new = [token for token in checked
                      if token in after["forced"] and token not in before["forced"]]
        if forced_new:
            core, _ = inference.minimal_core(checked, active + drafts,
                                             targets=set(forced_new))
            causing = sorted({c["version"] for c in core
                              if c.get("kind") != "draft"})
            if any(c.get("kind") == "draft" for c in core):
                causing.append(version)
            detail = {
                "affected_tokens": forced_new,
                "causing_versions": causing,
                "stale_versions": stale_versions,
                "constraints": inference.summary(core),
                "domains": {token: after["domains"][token] for token in forced_new},
            }
            _freeze(db, key, version, form, snapshot, groups, "inference", detail,
                    totals=totals, latest=latest, precision=precision)
            return _application(db, key, version, "frozen", "inference", form,
                                snapshot, detail)

        detail = {"affected_tokens": [], "causing_versions": [],
                  "stale_versions": stale_versions,
                  "constraints": inference.summary(active + drafts)}
        for group_index, group in enumerate(groups):
            total, mean = totals[group_index]
            _insert_publication(db, key, version, group_index, form, snapshot,
                                group, total, mean, precision, "published",
                                "aggregate_released", detail, latest,
                                publish_total=publish_total)
        return _application(db, key, version, "published", "aggregate_released",
                            form, snapshot, detail)


# ---------------------------------------------------------------------------
# Read side: public is sanitized, audit is internal
# ---------------------------------------------------------------------------

def public_report(db, course_id, year, form_id):
    """Published aggregates only; no token names, group list or versions."""
    published = [row for row in _published_rows(db, course_id, year, form_id)
                 if row["decision"] == "published"]
    return {"course_id": course_id, "year": year, "form_id": form_id,
            "results": [_public_entry(row, index + 1)
                        for index, row in enumerate(published)]}


def audit(db, course_id, year, form_id):
    """Full internal publication audit including frozen applications."""
    publications = _published_rows(db, course_id, year, form_id)
    applications = {}
    for publication in publications:
        entry = applications.setdefault(publication["version"], {
            "version": publication["version"],
            "decision": publication["decision"],
            "reason": publication["reason"],
            "form_version": publication["form_version"],
            "catalog": {"decision": publication["catalog_decision"],
                        "reason": publication["catalog_reason"],
                        "event_id": publication["catalog_event_id"]},
            "groups": [],
            "affected_tokens": publication["detail"].get("affected_tokens", []),
            "causing_versions": publication["detail"].get("causing_versions", []),
            "stale_versions": publication["detail"].get("stale_versions", []),
            "constraints": publication["detail"].get("constraints", []),
            "domains": publication["detail"].get("domains", {}),
            "created_at": publication["created_at"],
        })
        entry["groups"].append({
            "group_index": publication["group_index"],
            "tokens": publication["tokens"],
            "responses": publication["responses"],
            "total_score": publication["total_score"],
            "mean_score": publication["mean_score"],
            "precision": publication["precision"],
        })
    evidence = rows(
        db,
        "SELECT course_id,year,catalog_decision,catalog_reason,catalog_event_id,"
        "source,created_at FROM survey_catalog_evidence "
        "WHERE course_id=? AND year=? ORDER BY evidence_id",
        (course_id, year),
    )
    return {"course_id": course_id, "year": year, "form_id": form_id,
            "applications": list(applications.values()),
            "catalog_evidence": evidence}
