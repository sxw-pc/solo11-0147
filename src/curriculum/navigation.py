"""Check the simplest accessible label coverage for a handout."""


def check(db, doc_id):
    nodes = db.execute('SELECT * FROM handout_node WHERE doc_id=? ORDER BY node_id',
                       (doc_id,)).fetchall()
    if not nodes:
        return {'doc_id': doc_id, 'status': 'missing'}
    missing = [n['node_id'] for n in nodes if not n['label'].strip()]
    status = 'ready' if not missing else 'missing_labels'
    with db:
        db.execute('INSERT INTO handout_case VALUES (?,?,?,?,?) ON CONFLICT(doc_id) '
                   'DO UPDATE SET status=excluded.status,reason=excluded.reason,'
                   'node_count=excluded.node_count,source_event=excluded.source_event',
                   (doc_id, status, ','.join(missing), len(nodes), nodes[0]['source_event']))
    return {'doc_id': doc_id, 'status': status, 'missing_labels': missing}


def trace(db, doc_id, start_id, actions):
    """Follow the literal stored links; return currently has no caller context."""
    nodes = {row['node_id']: row for row in db.execute(
        'SELECT * FROM handout_node WHERE doc_id=?', (doc_id,)).fetchall()}
    if start_id not in nodes:
        raise ValueError('start node not found')
    current = start_id
    path = [current]
    targets = {'next': 'next_id', 'open': 'link_target', 'return': 'return_to'}
    for action in actions:
        if action not in targets:
            raise ValueError('unknown navigation action')
        target = nodes[current][targets[action]]
        if target not in nodes:
            raise ValueError('navigation target not found')
        current = target
        path.append(current)
    return {'doc_id': doc_id, 'path': path, 'end': current}
