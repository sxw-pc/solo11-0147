"""Capture a simple handout's declared reading nodes and links."""

from .sources import record


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
            db.execute('DELETE FROM handout_node WHERE doc_id=?', (doc_id,))
            for n in nodes:
                db.execute('INSERT INTO handout_node VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (doc_id, n['id'], event['course_id'], event['year'], n['kind'],
                     n.get('label', ''), n.get('next_id'), n.get('link_target'),
                     n.get('return_to'), event['event_id']))
            db.execute('DELETE FROM handout_case WHERE doc_id=?', (doc_id,))
            imported += 1
    return {'imported': imported}
