"""Stateful keyboard navigation and per-contract handout delivery decisions.

A reading service is not proven by non-empty labels.  Two pre-agreed contracts
are evaluated against one signed handout revision:

* ``linear_reading``: from the signed start, next/previous must walk the main
  line to the end, every required node is covered and every table cell keeps
  its row/column header context; explicitly optional footnotes may be skipped.
* ``interactive_reading``: additionally every keyboard-openable reference can
  be opened, nested references obey LIFO, formula branches each return to the
  main line, and no node is reachable only by mouse.

The navigation machine remembers more than the current node: each open pushes
a frame holding the reference point, its table headers and the signed return
anchor, so the same footnote entered from two places returns to two different
next-reading positions.  Failures are reproducible keyboard sequences with the
erroneous focus and the signed handout version attached.
"""
from collections import deque
import json

from .store import rows

LINEAR_CONTRACT = 'linear_reading'
INTERACTIVE_CONTRACT = 'interactive_reading'
CONTRACTS = (LINEAR_CONTRACT, INTERACTIVE_CONTRACT)

# Hard violations are failed transitions (no keyboard action exists); wrong
# return landings stay non-hard so the trace keeps the reproducible full path.
HARD_VIOLATIONS = {
    'missing_link', 'broken_link', 'mouse_only_link',
    'return_without_frame', 'no_previous',
}


def _load(db, doc_id, source_event=None):
    if source_event is None:
        current = db.execute('SELECT * FROM handout_current WHERE doc_id=?',
                             (doc_id,)).fetchone()
        if current is None:
            return None
        source_event = current['source_event']
    rev = db.execute('SELECT * FROM handout_revision WHERE doc_id=? AND source_event=?',
                     (doc_id, source_event)).fetchone()
    if rev is None:
        raise ValueError('handout revision not found')
    node_rows = rows(db, 'SELECT * FROM handout_node WHERE doc_id=? AND source_event=?',
                     (doc_id, source_event))
    if not node_rows:
        return None
    nodes = {}
    for r in node_rows:
        r['optional'] = bool(r['optional'])
        nodes[r['node_id']] = r
    edges = {}
    for r in rows(db, 'SELECT * FROM handout_link WHERE doc_id=? AND source_event=?',
                  (doc_id, source_event)):
        r['keyboard'] = bool(r['keyboard'])
        edges[(r['from_node'], r['action'])] = r
    start_id = rev['start_id']
    if start_id is None:
        first = db.execute('SELECT node_id FROM handout_node WHERE doc_id=? AND source_event=? '
                           'ORDER BY rowid LIMIT 1', (doc_id, source_event)).fetchone()
        start_id = first['node_id'] if first else None
    return {'doc_id': doc_id, 'source_event': source_event,
            'revision': rev['revision'], 'start_id': start_id,
            'nodes': nodes, 'edges': edges}


def _mainline(model):
    """Signed main-line order via next_id, with cycle protection."""
    order, seen, current = [], set(), model['start_id']
    nodes = model['nodes']
    while current is not None:
        if current not in nodes:
            return order, current
        if current in seen:
            return order, ('cycle', current)
        seen.add(current)
        order.append(current)
        current = nodes[current]['next_id']
    return order, None


def _required_anchor(model, entry_id):
    """The next reading position a correct return must restore."""
    nodes = model['nodes']
    entry = nodes[entry_id]
    if entry['next_id']:
        return entry['next_id']
    if entry['kind'] == 'table_cell' and entry['table_id']:
        head = next((n for n in nodes.values()
                     if n['kind'] == 'table_head' and n['table_id'] == entry['table_id']), None)
        if head and head['next_id']:
            return head['next_id']
    # A reference on a terminal node can only restore the reference itself.
    return entry_id


def _focus(model, node_id, depth, entry_from, frames):
    n = model['nodes'].get(node_id)
    position = model['mainline_index'].get(node_id)
    return {'node': node_id, 'depth': depth, 'entry_from': entry_from,
            'table_id': n['table_id'] if n else None,
            'row_header': n['row_header'] if n else None,
            'col_header': n['col_header'] if n else None,
            'mainline_position': position}


def _run(model, start_id, actions):
    """Pure stateful machine. Never raises; records every wrong landing."""
    nodes, edges = model['nodes'], model['edges']
    if start_id not in nodes:
        raise ValueError('start node not found')
    frames = []
    root_history = []
    level_nodes = {0: {start_id}}
    focus = _focus(model, start_id, 0, None, frames)
    focuses = [focus]
    violations = []

    def record(code, action, **extra):
        item = {'code': code, 'action': action, 'step': len(focuses) - 1,
                'focus': dict(focus), **extra}
        violations.append(item)
        return item

    for raw in actions:
        action = raw.strip()
        is_ref = action == 'open' or action.startswith('branch:')
        if action not in ('next', 'prev', 'return') and not is_ref:
            raise ValueError('unknown navigation action')

        if action == 'return':
            if not frames:
                record('return_without_frame', action)
                continue
            frame = frames[-1]
            from_node = focus['node']
            signed = frame['return_to']
            expected = _required_anchor(model, frame['entry'])
            if signed is None or signed not in nodes:
                record('broken_return', action, signed_anchor=signed,
                       expected_anchor=expected)
                continue
            closed_targets = {f['target'] for f in frames}
            frames.pop()
            if signed == from_node:
                code = 'return_loop'
            elif frames:
                # LIFO: one return closes one frame and lands on the parent.
                code = None if signed == frames[-1]['target'] else 'nested_return_bypass'
            elif signed != expected:
                code = 'return_loop' if signed in closed_targets else 'wrong_return_anchor'
            else:
                code = None
            depth = next((i + 1 for i, f in enumerate(frames) if f['target'] == signed), 0)
            entry_from = frames[-1]['entry'] if frames else None
            focus = _focus(model, signed, depth, entry_from, frames)
            level_nodes.setdefault(depth, set()).add(signed)
            focuses.append(focus)
            if code:
                record(code, action, signed_anchor=signed, expected_anchor=expected,
                       entry=frame['entry'])
            continue

        if is_ref:
            edge = edges.get((focus['node'], action))
            if edge is None:
                record('missing_link', action, entry=focus['node'])
                continue
            if not edge['keyboard']:
                record('mouse_only_link', action, entry=focus['node'],
                       target=edge['target'])
                continue
            if edge['target'] not in nodes:
                record('broken_link', action, entry=focus['node'], target=edge['target'])
                continue
            frame = {'entry': focus['node'], 'target': edge['target'],
                     'action': action, 'return_to': edge['return_to'],
                     'history': [], 'entry_table': (focus['table_id'],
                                                    focus['row_header'],
                                                    focus['col_header'])}
            frames.append(frame)
            focus = _focus(model, edge['target'], len(frames), frame['entry'], frames)
            level_nodes.setdefault(len(frames), set()).add(edge['target'])
            focuses.append(focus)
            continue

        if action == 'prev':
            history = frames[-1]['history'] if frames else root_history
            if not history:
                record('no_previous', action)
                continue
            focus = history.pop()
            focuses.append(focus)
            continue

        # next: the signed main-line edge of the current node.
        target = nodes[focus['node']]['next_id']
        if target is None:
            record('missing_link', action, entry=focus['node'])
            continue
        if target not in nodes:
            record('broken_link', action, entry=focus['node'], target=target)
            continue
        history = frames[-1]['history'] if frames else root_history
        history.append(focus)
        focus = _focus(model, target, focus['depth'], focus['entry_from'], frames)
        level_nodes.setdefault(focus['depth'], set()).add(target)
        focuses.append(focus)

    return {'doc_id': model['doc_id'], 'source_event': model['source_event'],
            'revision': model['revision'], 'start': start_id,
            'path': [f['node'] for f in focuses], 'focuses': focuses,
            'end': focus['node'], 'violations': violations}


def trace(db, doc_id, start_id, actions, source_event=None):
    """Follow the signed links with full entry history; hard failures raise."""
    model = _load(db, doc_id, source_event)
    if model is None:
        raise ValueError('handout not found')
    model['mainline_index'] = {n: i for i, n in enumerate(_mainline(model)[0])}
    result = _run(model, start_id, list(actions))
    hard = [v for v in result['violations'] if v['code'] in HARD_VIOLATIONS]
    if hard:
        raise ValueError(f"{hard[0]['code']} at {hard[0]['focus']['node']}")
    return result


def _keyboard_graph(model):
    adj = {}
    for node_id, n in model['nodes'].items():
        outs = []
        if n['next_id']:
            outs.append(('next', n['next_id']))
        for (frm, action), edge in model['edges'].items():
            if frm == node_id and edge['keyboard']:
                outs.append((action, edge['target']))
        adj[node_id] = outs
    return adj


def _keyboard_path(model, target):
    """BFS keyboard-only action sequence from the start to target (no returns)."""
    adj = _keyboard_graph(model)
    queue = deque([(model['start_id'], [])])
    seen = {model['start_id']}
    while queue:
        node, path = queue.popleft()
        if node == target:
            return path
        for action, nxt in adj.get(node, ()):
            if nxt in model['nodes'] and nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, path + [action]))
    return None


def _witness(model, code, actions, **extra):
    item = {'code': code, 'actions': list(actions),
            'doc_id': model['doc_id'], 'source_event': model['source_event'],
            'revision': model['revision']}
    item.update(extra)
    return item


def _reference_edges(model):
    out = []
    for (frm, action), edge in model['edges'].items():
        if action == 'open' or action.startswith('branch:'):
            out.append((frm, action, edge))
    return out


def _check_linear(model):
    witnesses, order, fault = [], *_mainline(model)
    if fault:
        if isinstance(fault, tuple):
            witnesses.append(_witness(model, 'mainline_cycle', ['next'],
                                      node=fault[1]))
        else:
            witnesses.append(_witness(model, 'broken_link', ['next'], node=fault))
    on_mainline = set(order)
    for node_id in sorted(model['nodes']):
        n = model['nodes'][node_id]
        if not n['optional'] and node_id not in on_mainline:
            witnesses.append(_witness(model, 'unreachable_node', [], node=node_id,
                                      detail='required node off the signed main line'))
    for node_id in order:
        n = model['nodes'][node_id]
        if n['kind'] == 'table_cell':
            missing = [name for name, val in (('table_id', n['table_id']),
                                              ('row_header', n['row_header']),
                                              ('col_header', n['col_header'])) if not val]
            if missing:
                witnesses.append(_witness(model, 'table_context_missing', ['next'],
                                          node=node_id, missing=missing))
    return witnesses


def _check_interactive(model):
    witnesses = []
    nodes, edges = model['nodes'], model['edges']
    adj = _keyboard_graph(model)
    reachable, queue = {model['start_id']}, deque([model['start_id']])
    while queue:
        node = queue.popleft()
        for _, nxt in adj.get(node, ()):
            if nxt in nodes and nxt not in reachable:
                reachable.add(nxt)
                queue.append(nxt)
    for node_id in sorted(nodes):
        if node_id not in reachable:
            witnesses.append(_witness(model, 'unreachable_node', [], node=node_id,
                                      detail='no keyboard path from the signed start'))

    for (frm, action), edge in sorted(edges.items()):
        if edge['target'] not in nodes:
            path = _keyboard_path(model, frm)
            if path is not None:
                witnesses.append(_witness(model, 'broken_link', path + [action],
                                          node=frm, target=edge['target']))
        if (action == 'open' or action.startswith('branch:')) and edge['return_to'] is None:
            witnesses.append(_witness(model, 'missing_return_anchor', [action],
                                      node=frm, target=edge['target']))

    for frm, action, edge in _reference_edges(model):
        if not edge['keyboard'] or edge['target'] not in nodes or frm not in reachable:
            continue
        path = _keyboard_path(model, frm)
        if path is None:
            continue
        expected = _required_anchor(model, frm)
        signed = edge['return_to']
        result = _run(model, model['start_id'], path + [action, 'return'])
        bad = [v for v in result['violations'] if v['action'] == 'return']
        landing = result['focuses'][-1]
        if bad:
            v = bad[0]
            witnesses.append(_witness(model, v['code'], path + [action, 'return'],
                                      entry=frm, signed_anchor=signed,
                                      expected_anchor=expected, focus=landing))
        elif signed != expected or landing['node'] != expected:
            witnesses.append(_witness(model, 'wrong_return_anchor',
                                      path + [action, 'return'], entry=frm,
                                      signed_anchor=signed, expected_anchor=expected,
                                      focus=landing))
        # Nested reference: LIFO must bring focus back through the inner note.
        inner = [e for (f2, a2), e in edges.items()
                 if f2 == edge['target'] and (a2 == 'open' or a2.startswith('branch:'))
                 and e['keyboard'] and e['target'] in nodes]
        for inner_edge in inner:
            inner_action = next(a2 for (f2, a2), e2 in edges.items() if e2 is inner_edge)
            inner_expected = _required_anchor(model, edge['target'])
            seq = path + [action, inner_action, 'return', 'return']
            nested = _run(model, model['start_id'], seq)
            nested_bad = [v for v in nested['violations']
                          if v['code'] in ('nested_return_bypass', 'return_loop',
                                           'wrong_return_anchor', 'broken_return')]
            inner_landing = nested['focuses'][-2]
            if inner_landing['node'] != inner_expected:
                witnesses.append(_witness(model, 'nested_return_bypass', seq,
                                          entry=edge['target'], focus=inner_landing,
                                          expected_anchor=inner_expected))
            elif nested_bad or nested['end'] != expected:
                v = nested_bad[0] if nested_bad else {'code': 'nested_return_bypass'}
                witnesses.append(_witness(model, v['code'], seq, entry=frm,
                                          signed_anchor=signed, expected_anchor=expected,
                                          focus=nested['focuses'][-1]))

    for node_id, n in sorted(nodes.items()):
        if n['kind'] != 'formula':
            continue
        branches = [(a, e) for (f2, a), e in edges.items()
                    if f2 == node_id and a.startswith('branch:') and e['keyboard']]
        if len(branches) < 2:
            witnesses.append(_witness(model, 'formula_branch_missing', [],
                                      node=node_id, branch_count=len(branches)))
            continue
        path = _keyboard_path(model, node_id)
        if path is None:
            continue
        expected = _required_anchor(model, node_id)
        for branch_action, edge in branches:
            seq = path + [branch_action, 'return']
            res = _run(model, model['start_id'], seq)
            if res['violations'] or res['end'] != expected:
                code = res['violations'][0]['code'] if res['violations'] else 'wrong_return_anchor'
                witnesses.append(_witness(model, code, seq, entry=node_id,
                                          signed_anchor=edge['return_to'],
                                          expected_anchor=expected,
                                          focus=res['focuses'][-1]))
    return witnesses


def check(db, doc_id, contracts=CONTRACTS, source_event=None):
    """Recompute the pre-agreed reading contracts for one handout revision."""
    model = _load(db, doc_id, source_event)
    if model is None:
        return {'doc_id': doc_id, 'status': 'missing', 'contracts': {}}
    model['mainline_index'] = {n: i for i, n in enumerate(_mainline(model)[0])}
    missing_labels = sorted(n['node_id'] for n in model['nodes'].values()
                            if not n['label'].strip())
    label_witness = _witness(model, 'missing_labels', [], nodes=missing_labels)
    checks = {LINEAR_CONTRACT: _check_linear, INTERACTIVE_CONTRACT: _check_interactive}
    result_contracts = {}
    with db:
        for contract in contracts:
            witnesses = list(checks[contract](model))
            if missing_labels:
                witnesses.insert(0, dict(label_witness))
            decision = 'deliverable' if not witnesses else 'blocked'
            reason = 'contract_satisfied' if not witnesses else witnesses[0]['code']
            result_contracts[contract] = {'contract': contract, 'decision': decision,
                                          'reason': reason, 'witnesses': witnesses}
            db.execute('INSERT OR IGNORE INTO handout_decision VALUES (?,?,?,?,?,?,?)',
                (doc_id, model['source_event'], contract, decision, reason,
                 json.dumps(witnesses, ensure_ascii=False, sort_keys=True),
                 len(model['nodes'])))
    status = 'ready' if all(c['decision'] == 'deliverable'
                            for c in result_contracts.values()) else 'blocked'
    return {'doc_id': doc_id, 'status': status,
            'source_event': model['source_event'], 'revision': model['revision'],
            'missing_labels': missing_labels, 'contracts': result_contracts}
