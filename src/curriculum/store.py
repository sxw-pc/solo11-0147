"""Durable independent catalog, graph and credit decisions."""
import sqlite3
from pathlib import Path

SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(event_id TEXT PRIMARY KEY,kind TEXT NOT NULL,object_id TEXT NOT NULL,event_time TEXT NOT NULL,sha256 TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS catalog_source(source_id TEXT PRIMARY KEY,department TEXT NOT NULL,valid_from TEXT NOT NULL,valid_until TEXT NOT NULL,source_event TEXT NOT NULL REFERENCES evidence(event_id));
CREATE TABLE IF NOT EXISTS course_revision(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),course_id TEXT NOT NULL,department TEXT NOT NULL,source_id TEXT NOT NULL,year INTEGER NOT NULL,version INTEGER NOT NULL,credits INTEGER NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS catalog_release(course_id TEXT NOT NULL,year INTEGER NOT NULL,event_id TEXT,decision TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(course_id,year));
CREATE TABLE IF NOT EXISTS prereq_rule(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),target_course TEXT NOT NULL,year INTEGER NOT NULL,rule_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS graph_revision(year INTEGER PRIMARY KEY,decision TEXT NOT NULL,cycle_json TEXT NOT NULL,source_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS qualification_case(student_id TEXT NOT NULL,course_id TEXT NOT NULL,year INTEGER NOT NULL,decision TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(student_id,course_id,year));
CREATE TABLE IF NOT EXISTS transcript_evidence(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),student_id TEXT NOT NULL,source_course TEXT NOT NULL,year INTEGER NOT NULL,credits INTEGER NOT NULL,grade INTEGER NOT NULL,issuer TEXT NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS equivalence_agreement(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),source_course TEXT NOT NULL,target_course TEXT NOT NULL,valid_from INTEGER NOT NULL,valid_until INTEGER NOT NULL,min_credits INTEGER NOT NULL,category TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS credit_award(student_id TEXT NOT NULL,target_course TEXT NOT NULL,year INTEGER NOT NULL,source_event TEXT NOT NULL REFERENCES evidence(event_id),decision TEXT NOT NULL,reason TEXT NOT NULL,category TEXT NOT NULL,PRIMARY KEY(student_id,target_course,year));
CREATE TABLE IF NOT EXISTS result_report(id INTEGER PRIMARY KEY AUTOINCREMENT,student_id TEXT NOT NULL,year INTEGER NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS outline_revision(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),course_id TEXT NOT NULL,year INTEGER NOT NULL,version INTEGER NOT NULL,issuer TEXT NOT NULL,objectives_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outline_release(course_id TEXT NOT NULL,year INTEGER NOT NULL,event_id TEXT,decision TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(course_id,year));
CREATE TABLE IF NOT EXISTS assessment_evidence(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),student_id TEXT NOT NULL,course_id TEXT NOT NULL,year INTEGER NOT NULL,objective_id TEXT NOT NULL,rubric_version INTEGER NOT NULL,score INTEGER NOT NULL,assessor TEXT NOT NULL,paper_id TEXT,status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS assessment_case(student_id TEXT NOT NULL,course_id TEXT NOT NULL,year INTEGER NOT NULL,objective_id TEXT NOT NULL,decision TEXT NOT NULL,source_event TEXT,PRIMARY KEY(student_id,course_id,year,objective_id));
CREATE TABLE IF NOT EXISTS anchor_calibration(event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),course_id TEXT NOT NULL,year INTEGER NOT NULL,objective_id TEXT NOT NULL,paper_id TEXT NOT NULL,rubric_version INTEGER NOT NULL,official_score INTEGER NOT NULL,issuer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batch_review_history(id INTEGER PRIMARY KEY AUTOINCREMENT,course_id TEXT NOT NULL,year INTEGER NOT NULL,objective_id TEXT NOT NULL,outline_version INTEGER NOT NULL,tolerance INTEGER NOT NULL,internal_decision TEXT NOT NULL,external_decision TEXT NOT NULL,reason_json TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL);
"""

def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA)
    # Additive migration for databases created before batch quality review existed.
    cols={r['name'] for r in db.execute('PRAGMA table_info(assessment_evidence)')}
    if 'paper_id' not in cols:db.execute('ALTER TABLE assessment_evidence ADD COLUMN paper_id TEXT')
    if 'status' not in cols:db.execute("ALTER TABLE assessment_evidence ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    return db

def rows(db,sql,args=()):return [dict(r) for r in db.execute(sql,args).fetchall()]
