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
CREATE TABLE IF NOT EXISTS handout_revision(doc_id TEXT NOT NULL,source_event TEXT NOT NULL REFERENCES evidence(event_id),revision INTEGER NOT NULL,start_id TEXT,PRIMARY KEY(doc_id,source_event));
CREATE TABLE IF NOT EXISTS handout_node(doc_id TEXT NOT NULL,source_event TEXT NOT NULL,node_id TEXT NOT NULL,course_id TEXT NOT NULL,year INTEGER NOT NULL,kind TEXT NOT NULL,label TEXT NOT NULL,next_id TEXT,optional INTEGER NOT NULL DEFAULT 0,table_id TEXT,row_header TEXT,col_header TEXT,PRIMARY KEY(doc_id,source_event,node_id));
CREATE TABLE IF NOT EXISTS handout_link(doc_id TEXT NOT NULL,source_event TEXT NOT NULL,from_node TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL,return_to TEXT,keyboard INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(doc_id,source_event,from_node,action));
CREATE TABLE IF NOT EXISTS handout_decision(doc_id TEXT NOT NULL,source_event TEXT NOT NULL,contract TEXT NOT NULL,decision TEXT NOT NULL,reason TEXT NOT NULL,witness_json TEXT NOT NULL,node_count INTEGER NOT NULL,PRIMARY KEY(doc_id,source_event,contract));
CREATE TABLE IF NOT EXISTS handout_current(doc_id TEXT PRIMARY KEY,source_event TEXT NOT NULL,revision INTEGER NOT NULL);
"""

def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);_migrate_handout(db);return db

def rows(db,sql,args=()):return [dict(r) for r in db.execute(sql,args).fetchall()]

def _migrate_handout(db):
    """Rebuild the first handout shape (one row per node, no revision history).

    Evidence bodies stay immutable; derived handout tables are re-shaped in
    place, preserving every signed node and link instead of dropping the ledger.
    """
    cols={r['name'] for r in db.execute('PRAGMA table_info(handout_node)').fetchall()}
    if cols and 'optional' not in cols:
        db.executescript("""
        CREATE TABLE handout_node_old AS SELECT * FROM handout_node;
        DROP TABLE handout_node;
        DROP TABLE IF EXISTS handout_link;
        DROP TABLE IF EXISTS handout_revision;
        DROP TABLE IF EXISTS handout_current;
        CREATE TABLE handout_node(doc_id TEXT NOT NULL,source_event TEXT NOT NULL,node_id TEXT NOT NULL,course_id TEXT NOT NULL,year INTEGER NOT NULL,kind TEXT NOT NULL,label TEXT NOT NULL,next_id TEXT,optional INTEGER NOT NULL DEFAULT 0,table_id TEXT,row_header TEXT,col_header TEXT,PRIMARY KEY(doc_id,source_event,node_id));
        INSERT INTO handout_node(doc_id,source_event,node_id,course_id,year,kind,label,next_id,optional)
        SELECT doc_id,source_event,node_id,course_id,year,kind,label,next_id,CASE kind WHEN 'footnote' THEN 1 ELSE 0 END FROM handout_node_old;
        CREATE TABLE handout_link(doc_id TEXT NOT NULL,source_event TEXT NOT NULL,from_node TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL,return_to TEXT,keyboard INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(doc_id,source_event,from_node,action));
        INSERT INTO handout_link(doc_id,source_event,from_node,action,target,return_to,keyboard)
        SELECT o.doc_id,o.source_event,o.node_id,'open',o.link_target,
               COALESCE(o.return_to,t.return_to),1
        FROM handout_node_old o LEFT JOIN handout_node_old t ON t.doc_id=o.doc_id AND t.node_id=o.link_target
        WHERE o.link_target IS NOT NULL;
        CREATE TABLE handout_revision(doc_id TEXT NOT NULL,source_event TEXT NOT NULL REFERENCES evidence(event_id),revision INTEGER NOT NULL,start_id TEXT,PRIMARY KEY(doc_id,source_event));
        INSERT OR IGNORE INTO handout_revision(doc_id,source_event,revision,start_id)
        SELECT DISTINCT doc_id,source_event,1,(
            SELECT node_id FROM handout_node_old s WHERE s.doc_id=o.doc_id LIMIT 1)
        FROM handout_node_old o;
        CREATE TABLE IF NOT EXISTS handout_current(doc_id TEXT PRIMARY KEY,source_event TEXT NOT NULL,revision INTEGER NOT NULL);
        INSERT OR IGNORE INTO handout_current(doc_id,source_event,revision)
        SELECT DISTINCT doc_id,source_event,1 FROM handout_node_old;
        DROP TABLE handout_node_old;
        """)
    case_cols={r['name'] for r in db.execute('PRAGMA table_info(handout_case)').fetchall()}
    if case_cols:
        # Replaced by handout_decision history; the old cache was fully derivable.
        db.execute('DROP TABLE handout_case')
    db.commit()
