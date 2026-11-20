"""Capture author-signed handout nodes, keyboard links and return anchors.

Each intake event is one signed revision of a handout.  Nodes carry their
reading role and table header context; every reference edge (footnote open,
formula branch, mouse-only pointer) is stored separately so the same footnote
can be entered from two reference points with its own return anchor.  Raw
events stay immutable in ``evidence``; revisions are never overwritten.
"""

from .sources import record

NODE_KINDS = {'text', 'footnote', 'formula', 'table_head', 'table_cell'}
KEYBOARD_ACTIONS = {'open', 'pointer'}


def ingest(db, events):
    imported = 0
    with db:
        for event in events:
            doc_id = event['doc_id']
            if not record(db, event, 'handout', doc_id):
                continue
            nodes = event['nodes']
            ids = [n['id'] for n in nodes]
            if not ids or len(set(ids)) != len(ids):
                raise ValueError('unique handout nodes required')
            for n in nodes:
                if n.get('kind', 'text') not in NODE_KINDS:
                    raise ValueError(f"unknown handout node kind: {n.get('kind')}")
                for link in n.get('links', ()):
                    action = link['action']
                    if action not in KEYBOARD_ACTIONS and not action.startswith('branch:'):
                        raise ValueError(f'unknown link action: {action}')
            revision = 1 + db.execute(
                'SELECT COALESCE(MAX(revision),0) FROM handout_revision WHERE doc_id=?',
                (doc_id,)).fetchone()[0]
            source_event = event['event_id']
            by_id = {n['id']: n for n in nodes}
            for n in nodes:
                optional = n.get('optional', n.get('kind') == 'footnote')
                db.execute('INSERT INTO handout_node VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (doc_id, source_event, n['id'], event['course_id'], event['year'],
                     n.get('kind', 'text'), n.get('label', ''), n.get('next_id'),
                     1 if optional else 0, n.get('table_id'),
                     n.get('row_header'), n.get('col_header')))
                # Legacy intake shape: a single open edge carried on the node;
                # the return anchor was signed on the footnote target itself.
                if n.get('link_target') is not None:
                    target_node = by_id.get(n['link_target'])
                    legacy_anchor = n.get('return_to')
                    if legacy_anchor is None and target_node is not None:
                        legacy_anchor = target_node.get('return_to')
                    db.execute('INSERT INTO handout_link VALUES (?,?,?,?,?,?,?)',
                        (doc_id, source_event, n['id'], 'open', n['link_target'],
                         legacy_anchor, 1))
                for link in n.get('links', ()):
                    db.execute('INSERT INTO handout_link VALUES (?,?,?,?,?,?,?)',
                        (doc_id, source_event, n['id'], link['action'], link['target'],
                         link.get('return_to'), 0 if link.get('keyboard') is False else 1))
            start_id = event.get('start_id') or ids[0]
            db.execute('INSERT INTO handout_revision VALUES (?,?,?,?)',
                (doc_id, source_event, revision, start_id))
            imported += 1
        # The "current" revision is the newest signed event per event_time;
        # out-of-order ingestion still points the service at the latest one.
        for row in db.execute('SELECT DISTINCT doc_id FROM handout_revision').fetchall():
            latest = db.execute(
                'SELECT r.doc_id,r.source_event,r.revision FROM handout_revision r '
                "JOIN evidence e ON e.event_id=r.source_event WHERE r.doc_id=? "
                'ORDER BY e.event_time DESC,r.revision DESC LIMIT 1',
                (row['doc_id'],)).fetchone()
            db.execute('INSERT INTO handout_current VALUES (?,?,?) '
                       'ON CONFLICT(doc_id) DO UPDATE SET source_event=excluded.source_event,'
                       'revision=excluded.revision',
                       (latest['doc_id'], latest['source_event'], latest['revision']))
    return {'imported': imported}
