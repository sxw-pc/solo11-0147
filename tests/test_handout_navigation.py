import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from curriculum import handout, navigation
from curriculum.store import connect

FIX = ROOT / 'fixtures' / 'curriculum'


def load(name):
    return json.loads((FIX / name).read_text(encoding='utf-8'))


def revised_event(event_id='handout-2'):
    event = copy.deepcopy(load('handout_revised.json')[0])
    event['event_id'] = event_id
    return event


class HandoutCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'test.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def contracts(self, event, doc_id='C3-notes'):
        handout.ingest(self.db, [event])
        result = navigation.check(self.db, doc_id)
        return result

    # --- the four-node handout accepted in the task statement ----------

    def test_legacy_four_node_linear_pass_interactive_blocked(self):
        data = load('handout_good.json')
        self.assertEqual(handout.ingest(self.db, data)['imported'], 1)
        # Literal replay still walks the stored links with full entry history.
        trace = navigation.trace(self.db, 'C3-notes', 'intro',
                                 ['next', 'open', 'return'])
        self.assertEqual(trace['path'], ['intro', 'proof', 'note', 'intro'])
        self.assertEqual([v['code'] for v in trace['violations']],
                         ['wrong_return_anchor'])
        self.assertEqual(trace['violations'][0]['signed_anchor'], 'intro')
        self.assertEqual(trace['violations'][0]['expected_anchor'], 'end')
        check = navigation.check(self.db, 'C3-notes')
        self.assertEqual(check['status'], 'blocked')
        linear = check['contracts'][navigation.LINEAR_CONTRACT]
        interactive = check['contracts'][navigation.INTERACTIVE_CONTRACT]
        self.assertEqual(linear['decision'], 'deliverable')
        self.assertEqual(interactive['decision'], 'blocked')
        witness = interactive['witnesses'][0]
        self.assertEqual(witness['code'], 'wrong_return_anchor')
        self.assertEqual(witness['actions'], ['next', 'open', 'return'])
        self.assertEqual(witness['source_event'], 'handout-1')
        self.assertEqual(witness['focus']['node'], 'intro')
        # Labels alone can never release the service.
        self.assertEqual(check['missing_labels'], [])

    def test_missing_label_blocks_both_contracts(self):
        data = load('handout_good.json')
        data[0]['nodes'][2]['label'] = ''
        data[0]['event_id'] = 'handout-empty'
        handout.ingest(self.db, data)
        check = navigation.check(self.db, 'C3-notes')
        self.assertEqual(check['missing_labels'], ['note'])
        for contract in check['contracts'].values():
            self.assertEqual(contract['decision'], 'blocked')
            self.assertEqual(contract['witnesses'][0]['code'], 'missing_labels')

    def test_idempotent_reingest_keeps_nodes_and_decisions(self):
        data = load('handout_good.json')
        handout.ingest(self.db, data)
        navigation.check(self.db, 'C3-notes')
        self.assertEqual(handout.ingest(self.db, data)['imported'], 0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM handout_node WHERE source_event='handout-1'").fetchone()[0], 4)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM handout_decision").fetchone()[0], 2)

    def test_same_event_id_different_body_is_rejected(self):
        handout.ingest(self.db, load('handout_good.json'))
        changed = load('handout_good.json')[0]
        changed['nodes'][0]['label'] = 'Tampered'
        with self.assertRaises(ValueError):
            handout.ingest(self.db, [changed])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0], 1)

    # --- correction history --------------------------------------------

    def test_correction_recomputes_current_but_keeps_old_witness(self):
        handout.ingest(self.db, load('handout_good.json'))
        first = navigation.check(self.db, 'C3-notes')
        self.assertEqual(first['revision'], 1)
        handout.ingest(self.db, [revised_event()])
        second = navigation.check(self.db, 'C3-notes')
        self.assertEqual((second['revision'], second['status']), (2, 'ready'))
        old = self.db.execute(
            "SELECT contract,decision,reason,witness_json FROM handout_decision "
            "WHERE doc_id='C3-notes' AND source_event='handout-1' "
            "AND contract='interactive_reading'").fetchone()
        self.assertEqual((old['decision'], old['reason']),
                         ('blocked', 'wrong_return_anchor'))
        self.assertIn('next', json.loads(old['witness_json'])[0]['actions'])
        current = self.db.execute(
            'SELECT source_event,revision FROM handout_current').fetchone()
        self.assertEqual(tuple(current), ('handout-2', 2))
        # Both signed revisions remain replayable.
        old_trace = navigation.trace(self.db, 'C3-notes', 'intro',
                                     ['next', 'open', 'return'],
                                     source_event='handout-1')
        self.assertEqual(old_trace['path'], ['intro', 'proof', 'note', 'intro'])
        new_trace = navigation.trace(self.db, 'C3-notes', 'proof',
                                     ['open', 'return'], source_event='handout-2')
        self.assertEqual(new_trace['path'], ['proof', 'note', 'formula'])

    # --- same footnote, two reference points ---------------------------

    def test_same_note_two_entries_restores_each_context(self):
        check = self.contracts(revised_event())
        self.assertEqual(check['status'], 'ready')
        from_proof = navigation.trace(self.db, 'C3-notes', 'proof',
                                      ['open', 'return'], source_event='handout-2')
        self.assertEqual(from_proof['path'], ['proof', 'note', 'formula'])
        from_cell = navigation.trace(self.db, 'C3-notes', 'cell-1',
                                     ['open', 'return'], source_event='handout-2')
        self.assertEqual(from_cell['path'], ['cell-1', 'note', 'cell-2'])
        landing = from_cell['focuses'][-1]
        self.assertEqual((landing['row_header'], landing['col_header']),
                         ('Case two', 'Outcome B'))
        self.assertEqual(landing['mainline_position'], 5)
        # Wrong shared anchor would block the interactive contract.
        broken = revised_event('handout-shared-bad')
        for n in broken['nodes']:
            if n['id'] == 'cell-1':
                n['links'][0]['return_to'] = 'formula'
        result = self.contracts(broken)
        interactive = result['contracts'][navigation.INTERACTIVE_CONTRACT]
        self.assertEqual(interactive['decision'], 'blocked')
        self.assertTrue(any(w['code'] == 'wrong_return_anchor' and w['entry'] == 'cell-1'
                            for w in interactive['witnesses']))

    # --- nested footnote LIFO ------------------------------------------

    def test_nested_footnote_lifo_return(self):
        self.contracts(revised_event())
        trace = navigation.trace(self.db, 'C3-notes', 'proof',
                                 ['open', 'open', 'return', 'return'],
                                 source_event='handout-2')
        self.assertEqual(trace['path'],
                         ['proof', 'note', 'note-2', 'note', 'formula'])
        self.assertEqual([f['depth'] for f in trace['focuses']],
                         [0, 1, 2, 1, 0])
        self.assertEqual(trace['violations'], [])
        # A second-level return that jumps straight back to the main line fails.
        broken = revised_event('handout-lifo-bad')
        for n in broken['nodes']:
            if n['id'] == 'note':
                n['links'][0]['return_to'] = 'formula'
        result = self.contracts(broken)
        codes = {w['code'] for w in
                 result['contracts'][navigation.INTERACTIVE_CONTRACT]['witnesses']}
        self.assertIn('nested_return_bypass', codes)

    # --- table header context ------------------------------------------

    def test_table_cells_keep_row_and_column_headers(self):
        check = self.contracts(revised_event())
        linear = check['contracts'][navigation.LINEAR_CONTRACT]
        self.assertEqual(linear['decision'], 'deliverable')
        trace = navigation.trace(self.db, 'C3-notes', 'intro',
                                 ['next', 'next', 'next', 'next', 'next', 'next'],
                                 source_event='handout-2')
        cells = [f for f in trace['focuses'] if f['table_id']]
        self.assertEqual([(f['node'], f['row_header'], f['col_header']) for f in cells],
                         [('tbl-head', 'Case', 'Outcome'),
                          ('cell-1', 'Case one', 'Outcome A'),
                          ('cell-2', 'Case two', 'Outcome B')])
        stripped = revised_event('handout-header-bad')
        stripped['nodes'][4]['col_header'] = ''
        result = self.contracts(stripped)
        witness = result['contracts'][navigation.LINEAR_CONTRACT]['witnesses'][0]
        self.assertEqual((witness['code'], witness['node'], witness['missing']),
                         ('table_context_missing', 'cell-1', ['col_header']))

    # --- formula branches ----------------------------------------------

    def test_formula_two_branches_each_return_to_main_line(self):
        self.contracts(revised_event())
        for action, target in (('branch:alpha', 'branch-a'), ('branch:beta', 'branch-b')):
            trace = navigation.trace(self.db, 'C3-notes', 'formula',
                                     [action, 'return'], source_event='handout-2')
            self.assertEqual(trace['path'], ['formula', target, 'tbl-head'])
            self.assertEqual(trace['violations'], [])
        broken = revised_event('handout-branch-bad')
        broken['nodes'][2]['links'][0]['return_to'] = 'end'
        result = self.contracts(broken)
        codes = {w['code'] for w in
                 result['contracts'][navigation.INTERACTIVE_CONTRACT]['witnesses']}
        self.assertIn('wrong_return_anchor', codes)
        one_branch = revised_event('handout-one-branch')
        one_branch['nodes'][2]['links'].pop()
        result = self.contracts(one_branch)
        self.assertTrue(any(w['code'] == 'formula_branch_missing'
                            for w in result['contracts']
                            [navigation.INTERACTIVE_CONTRACT]['witnesses']))

    # --- keyboard unreachable island -----------------------------------

    def test_mouse_only_island_is_not_released_by_labels(self):
        event = revised_event('handout-island')
        event['nodes'][0]['links'] = [
            {'action': 'pointer', 'target': 'island', 'keyboard': False}]
        event['nodes'].append(
            {'id': 'island', 'kind': 'text', 'label': 'Mouse-only aside',
             'optional': True})
        result = self.contracts(event)
        self.assertEqual(result['contracts'][navigation.LINEAR_CONTRACT]['decision'],
                         'deliverable')
        interactive = result['contracts'][navigation.INTERACTIVE_CONTRACT]
        self.assertEqual(interactive['decision'], 'blocked')
        self.assertEqual(interactive['witnesses'][0]['code'], 'unreachable_node')
        self.assertEqual(interactive['witnesses'][0]['node'], 'island')
        with self.assertRaises(ValueError):
            navigation.trace(self.db, 'C3-notes', 'intro',
                             ['pointer'], source_event='handout-island')

    # --- return ring ----------------------------------------------------

    def test_return_ring_loses_the_return_action(self):
        event = revised_event('handout-ring')
        for n in event['nodes']:
            if n['id'] == 'cell-2':
                n['links'][0]['return_to'] = 'note-2'
        result = self.contracts(event)
        codes = {w['code'] for w in
                 result['contracts'][navigation.INTERACTIVE_CONTRACT]['witnesses']}
        self.assertIn('return_loop', codes)
        trace = navigation.trace(self.db, 'C3-notes', 'cell-2',
                                 ['open', 'return'], source_event='handout-ring')
        self.assertEqual(trace['path'], ['cell-2', 'note-2', 'note-2'])
        self.assertEqual(trace['violations'][0]['code'], 'return_loop')

    # --- previous / next actual transitions -----------------------------

    def test_previous_walks_back_per_frame(self):
        self.contracts(revised_event())
        trace = navigation.trace(self.db, 'C3-notes', 'intro',
                                 ['next', 'next', 'prev', 'next'],
                                 source_event='handout-2')
        self.assertEqual(trace['path'],
                         ['intro', 'proof', 'formula', 'proof', 'formula'])
        # Previous inside a freshly opened note has its own empty frame:
        # frame history is independent of the main-line trail above.
        with self.assertRaises(ValueError) as ctx:
            navigation.trace(self.db, 'C3-notes', 'proof',
                             ['open', 'prev'], source_event='handout-2')
        self.assertIn('no_previous', str(ctx.exception))

    def test_broken_keyboard_link_is_witnessed(self):
        event = revised_event('handout-broken')
        event['nodes'][1]['links'] = [
            {'action': 'open', 'target': 'ghost', 'return_to': 'formula'}]
        result = self.contracts(event)
        witness = result['contracts'][navigation.INTERACTIVE_CONTRACT]['witnesses'][0]
        self.assertEqual((witness['code'], witness['target']),
                         ('broken_link', 'ghost'))

    # --- original event replay and immutability -------------------------

    def test_original_revision_replay_after_rebuild(self):
        handout.ingest(self.db, load('handout_good.json'))
        handout.ingest(self.db, [revised_event()])
        original_body = self.db.execute(
            "SELECT body FROM evidence WHERE event_id='handout-1'").fetchone()[0]
        self.assertIn('"return_to":"intro"', original_body)
        check = navigation.check(self.db, 'C3-notes', source_event='handout-1')
        self.assertEqual(check['contracts'][navigation.INTERACTIVE_CONTRACT]
                         ['witnesses'][0]['source_event'], 'handout-1')
        check_v2 = navigation.check(self.db, 'C3-notes', source_event='handout-2')
        self.assertEqual(check_v2['status'], 'ready')

    def test_missing_document_reports_missing(self):
        self.assertEqual(navigation.check(self.db, 'no-such-doc')['status'],
                         'missing')

    def test_legacy_shape_migrates_from_first_schema(self):
        # Simulate a database built by the original single-table handout shape.
        self.db.executescript("""
        DROP TABLE handout_node; DROP TABLE handout_link; DROP TABLE handout_revision;
        CREATE TABLE handout_node(doc_id TEXT NOT NULL,node_id TEXT NOT NULL,course_id TEXT NOT NULL,
          year INTEGER NOT NULL,kind TEXT NOT NULL,label TEXT NOT NULL,next_id TEXT,
          link_target TEXT,return_to TEXT,source_event TEXT NOT NULL,
          PRIMARY KEY(doc_id,node_id));
        """)
        raw = load('handout_good.json')[0]
        self.db.execute("INSERT INTO evidence VALUES ('handout-1','handout','C3-notes',?,?,?)",
                        (raw['event_time'], 'x', json.dumps(raw, sort_keys=True)))
        for n in raw['nodes']:
            self.db.execute('INSERT INTO handout_node VALUES (?,?,?,?,?,?,?,?,?,?)',
                (raw['doc_id'], n['id'], raw['course_id'], raw['year'], n['kind'],
                 n['label'], n.get('next_id'), n.get('link_target'),
                 n.get('return_to'), raw['event_id']))
        self.db.commit()
        from curriculum.store import connect as reconnect
        path = Path(self.tmp.name) / 'test.sqlite'
        self.db.close()
        db = reconnect(path)
        try:
            check = navigation.check(db, 'C3-notes')
            self.assertEqual(check['contracts'][navigation.LINEAR_CONTRACT]['decision'],
                             'deliverable')
            self.assertEqual(check['contracts']
                             [navigation.INTERACTIVE_CONTRACT]['decision'], 'blocked')
            link = db.execute("SELECT * FROM handout_link WHERE from_node='proof'").fetchone()
            self.assertEqual((link['target'], link['return_to'], bool(link['keyboard'])),
                             ('note', 'intro', True))
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
