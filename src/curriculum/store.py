"""Durable independent catalog, graph and credit decisions."""
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(event_id TEXT PRIMARY KEY,kind TEXT NOT NULL,object_id TEXT NOT NULL,event_time TEXT NOT NULL,sha256 TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS catalog_source(source_id TEXT PRIMARY KEY,department TEXT NOT NULL,valid_from TEXT NOT NULL,valid_until TEXT NOT NULL,source_event TEXT NOT NULL REFERENCES evidence(event_id));
CREATE TABLE IF NOT EXISTS course_revision(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),course_id TEXT NOT NULL,department TEXT NOT NULL,source_id TEXT NOT NULL,year INTEGER NOT NULL,version INTEGER NOT NULL,credits INTEGER NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS catalog_release(course_id TEXT NOT NULL,year INTEGER NOT NULL,event_id TEXT,decision TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(course_id,year));
-- Catalog decisions are append-only so a later restoration can be audited
-- against the release that originally froze survey publication.
CREATE TABLE IF NOT EXISTS catalog_release_history(
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id TEXT NOT NULL,
    year INTEGER NOT NULL,
    event_id TEXT,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS prereq_rule(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),target_course TEXT NOT NULL,year INTEGER NOT NULL,rule_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS graph_revision(year INTEGER PRIMARY KEY,decision TEXT NOT NULL,cycle_json TEXT NOT NULL,source_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS qualification_case(student_id TEXT NOT NULL,course_id TEXT NOT NULL,year INTEGER NOT NULL,decision TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(student_id,course_id,year));
CREATE TABLE IF NOT EXISTS transcript_evidence(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),student_id TEXT NOT NULL,source_course TEXT NOT NULL,year INTEGER NOT NULL,credits INTEGER NOT NULL,grade INTEGER NOT NULL,issuer TEXT NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS equivalence_agreement(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),source_course TEXT NOT NULL,target_course TEXT NOT NULL,valid_from INTEGER NOT NULL,valid_until INTEGER NOT NULL,min_credits INTEGER NOT NULL,category TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS credit_award(student_id TEXT NOT NULL,target_course TEXT NOT NULL,year INTEGER NOT NULL,source_event TEXT NOT NULL REFERENCES evidence(event_id),decision TEXT NOT NULL,reason TEXT NOT NULL,category TEXT NOT NULL,PRIMARY KEY(student_id,target_course,year));
CREATE TABLE IF NOT EXISTS result_report(id INTEGER PRIMARY KEY AUTOINCREMENT,student_id TEXT NOT NULL,year INTEGER NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS survey_form(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),course_id TEXT NOT NULL,year INTEGER NOT NULL,form_id TEXT NOT NULL,version INTEGER NOT NULL,question TEXT NOT NULL,issuer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS survey_response(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),course_id TEXT NOT NULL,year INTEGER NOT NULL,form_id TEXT NOT NULL,response_token TEXT NOT NULL,score INTEGER NOT NULL,form_version INTEGER);
CREATE TABLE IF NOT EXISTS survey_case(course_id TEXT NOT NULL,year INTEGER NOT NULL,form_id TEXT NOT NULL,responses INTEGER NOT NULL,mean_score REAL,decision TEXT NOT NULL,form_version INTEGER,form_decision TEXT,catalog_decision TEXT,PRIMARY KEY(course_id,year,form_id));
-- Every public aggregate application keeps its own version.  Frozen or
-- rejected applications stay in this table as evidence but never become
-- constraints: only rows whose decision is 'published' are public.
CREATE TABLE IF NOT EXISTS survey_publication(
    publication_id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id TEXT NOT NULL,
    year INTEGER NOT NULL,
    form_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    group_index INTEGER NOT NULL,
    form_version INTEGER,
    responses INTEGER NOT NULL,
    total_score INTEGER,
    mean_score REAL,
    precision INTEGER NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    catalog_decision TEXT,
    catalog_reason TEXT,
    catalog_event_id TEXT,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS survey_publication_version
    ON survey_publication(course_id,year,form_id,version,group_index);
-- The token list is deliberately held only in this internal table (and in
-- detail_json); it is never selected by the public report helpers.
CREATE TABLE IF NOT EXISTS survey_publication_member(
    publication_id INTEGER NOT NULL REFERENCES survey_publication(publication_id),
    response_token TEXT NOT NULL,
    score INTEGER,
    PRIMARY KEY(publication_id,response_token)
);
-- Cross-ledger evidence: a catalog release that froze public survey
-- applications remains auditable even after the catalog is restored.
CREATE TABLE IF NOT EXISTS survey_catalog_evidence(
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id TEXT NOT NULL,
    year INTEGER NOT NULL,
    catalog_decision TEXT NOT NULL,
    catalog_reason TEXT NOT NULL,
    catalog_event_id TEXT,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# Columns added after the first released schema.  CREATE TABLE IF NOT EXISTS
# does not alter an existing table, so old databases are upgraded in place.
_ADDED_COLUMNS = {
    "survey_response": {"form_version": "INTEGER"},
    "survey_case": {
        "form_version": "INTEGER",
        "form_decision": "TEXT",
        "catalog_decision": "TEXT",
    },
}


def _migrate(db):
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, sql_type in columns.items():
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
    db.commit()


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    _migrate(db)
    return db


def rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args).fetchall()]
