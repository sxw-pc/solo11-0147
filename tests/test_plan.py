import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from curriculum import catalog, prereq, credit, degree, plan
from curriculum.store import connect, rows

T1, T2, T3 = 20261, 20262, 20263


def source_event():
    return {'event_id': 'src-d', 'op': 'source', 'source_id': 'dept-d', 'department': 'D',
            'valid_from': 2025, 'valid_until': 2030, 'event_time': '2026-01-01T00:00:00Z'}


def course(eid, course_id, credits, year=2026, version=1, status='open',
           time='2026-01-02T00:00:00Z'):
    return {'event_id': eid, 'op': 'course', 'course_id': course_id, 'department': 'D',
            'source_id': 'dept-d', 'year': year, 'version': version, 'credits': credits,
            'status': status, 'event_time': time}


def rule(eid, target, all_=(), any_=(), year=2026, time='2026-01-03T00:00:00Z'):
    return {'event_id': eid, 'op': 'rule', 'target_course': target, 'year': year,
            'rule': {'all': list(all_), 'any': list(any_)}, 'event_time': time}


def offer(eid, course_id, term, available=True, version=1, time='2026-01-05T00:00:00Z'):
    return {'event_id': eid, 'course_id': course_id, 'term': term, 'available': available,
            'version': version, 'event_time': time}


def req(eid, program, course_id):
    return {'event_id': eid, 'program_id': program, 'course_id': course_id,
            'event_time': '2026-01-04T00:00:00Z'}


def group(eid, program, group_id, options, choose=1):
    return {'event_id': eid, 'program_id': program, 'group_id': group_id, 'choose': choose,
            'options': list(options), 'event_time': '2026-01-04T00:00:00Z'}


def profile(eid, student, program, start=T1, cap=6):
    return {'event_id': eid, 'student_id': student, 'program_id': program,
            'start_term': start, 'max_credits': cap, 'event_time': '2026-01-04T00:00:00Z'}


def transcript(eid, student, source_course, credits=4, grade=80, status='passed',
               issuer='D', year=2026, time='2026-02-01T00:00:00Z'):
    return {'event_id': eid, 'op': 'transcript', 'student_id': student,
            'source_course': source_course, 'year': year, 'credits': credits,
            'grade': grade, 'issuer': issuer, 'status': status, 'event_time': time}


def agreement(eid, source, target, valid_from=2025, valid_until=2026, min_credits=2,
              category='required', time='2026-01-06T00:00:00Z'):
    return {'event_id': eid, 'op': 'agreement', 'source_course': source,
            'target_course': target, 'valid_from': valid_from, 'valid_until': valid_until,
            'min_credits': min_credits, 'category': category, 'event_time': time}


def scheduled_terms(verdict_section):
    if 'schedule' in verdict_section:
        schedule = verdict_section['schedule']
    else:
        assert len(verdict_section['branches']) == 1
        schedule = verdict_section['branches'][0]['schedule']
    return {c['course_id']: t['term'] for t in schedule for c in t['courses']}


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')

    def tearDown(self):
        self.db.close()

    def load_catalog(self, events):
        catalog.ingest(self.db, [source_event()] + events)

    def test_conditional_plan_and_promise_under_offer_dispute(self):
        # Acceptance sample: A,B in term 1, C,D in term 2; C needs A, D needs B.
        self.load_catalog([course('ca', 'A', 4), course('cb', 'B', 2),
                           course('cc', 'C', 4), course('cd', 'D', 2)])
        prereq.ingest(self.db, [rule('rc', 'C', all_=['A']), rule('rd', 'D', all_=['B'])])
        degree.ingest(self.db, {
            'requirements': [req('q1', 'P1', 'A'), req('q2', 'P1', 'B'),
                             req('q3', 'P1', 'C'), req('q4', 'P1', 'D')],
            'profiles': [profile('p1', 's1', 'P1')],
            'offers': [offer('oa1', 'A', T1), offer('ob1', 'B', T1),
                       offer('oc2', 'C', T2), offer('od2', 'D', T2)]})
        first = plan.latest(self.db, 's1', 'P1')
        self.assertEqual(first['promise']['term'], T2)

        # Same-version available/stopped dispute on B in term 1; B opens in
        # term 2 and D stays open in term 3.
        degree.ingest(self.db, {'offers': [offer('ob1-stop', 'B', T1, available=False),
                                           offer('ob2', 'B', T2), offer('od3', 'D', T3)]})
        verdict = plan.verify(self.db, 's1', 'P1', cause='manual')
        cond = verdict['conditional']
        self.assertEqual((cond['state'], cond['term']), ('planned', T2))
        self.assertEqual(cond['assumptions'],
                         [{'course_id': 'B', 'term': T1, 'assumed': 'available',
                           'version': 1, 'events': ['ob1', 'ob1-stop']}])
        self.assertEqual(scheduled_terms(cond), {'A': T1, 'B': T1, 'C': T2, 'D': T2})
        self.assertEqual(cond['infeasible_before'][0]['key_constraints'],
                         ['credit_cap', 'offering', 'prereq'])

        promise = verdict['promise']
        self.assertEqual((promise['state'], promise['term']), ('planned', T3))
        self.assertNotEqual(promise['term'], cond['term'])
        branches = {b['interpretation'][0]['available']: b for b in promise['branches']}
        self.assertEqual(branches[True]['term'], T2)
        # A per-course earliest-offering algorithm would place D in term 2;
        # in the stopped branch B only finishes in term 2, so D moves to term 3.
        self.assertEqual(branches[False]['term'], T3)
        self.assertEqual(scheduled_terms(branches[False])['D'], T3)
        earlier = {e['term']: e for e in promise['infeasible_before']}
        stopped_keys = [b['key_constraints'] for b in earlier[T2]['branches']
                        if not b['interpretation'][0]['available']]
        self.assertIn('prereq', stopped_keys[0])

        # The report keeps the catalog, graph, award and offer evidence used.
        ev = verdict['evidence']
        self.assertEqual(ev['catalog']['B']['2026']['credits'], 2)
        self.assertEqual(ev['graph']['2026']['decision'], 'published')
        self.assertEqual(ev['disputes'][0]['events'], ['ob1', 'ob1-stop'])
        self.assertIn({'event_id': 'ob1-stop', 'course_id': 'B', 'term': T1,
                       'available': 0, 'version': 1}, ev['offers'])
        # The old single-course and missing-count entries stay independently queryable.
        self.assertEqual(prereq.qualify(self.db, 's1', 'C', 2026, {'A'})['decision'], 'eligible')
        self.assertEqual(degree.decide(self.db, 's1', 'P1', 2026)['missing_count'], 4)

    def test_credit_cap_contention_defers_whole_chain(self):
        # Q,R are both AND-prerequisites of S; 4+4 credits cannot share one term.
        self.load_catalog([course('cq', 'Q', 4), course('cr', 'R', 4), course('cs', 'S', 4)])
        prereq.ingest(self.db, [rule('rs', 'S', all_=['Q', 'R'])])
        degree.ingest(self.db, {
            'requirements': [req('q1', 'P2', 'Q'), req('q2', 'P2', 'R'), req('q3', 'P2', 'S')],
            'profiles': [profile('p1', 's1', 'P2')],
            'offers': [offer('oq1', 'Q', T1), offer('oq2', 'Q', T2),
                       offer('or1', 'R', T1), offer('or2', 'R', T2),
                       offer('os2', 'S', T2), offer('os3', 'S', T3)]})
        verdict = plan.latest(self.db, 's1', 'P2')['promise']['body']['section']
        self.assertEqual(verdict['term'], T3)
        self.assertEqual(scheduled_terms(verdict), {'Q': T1, 'R': T2, 'S': T3})
        cond = plan.latest(self.db, 's1', 'P2')['conditional']['body']['section']
        earlier = {e['term']: e for e in cond['infeasible_before']}
        self.assertEqual(earlier[T2]['key_constraints'], ['credit_cap'])

    def test_and_or_prereq_differs_from_choice_group(self):
        self.load_catalog([course('cm', 'M', 2), course('cx', 'X', 2),
                           course('cy', 'Y', 2), course('cz', 'Z', 2)])
        prereq.ingest(self.db, [rule('rz', 'Z', all_=['M'], any_=['X', 'Y'])])
        degree.ingest(self.db, {
            'requirements': [req('q1', 'PA', 'M'), req('q2', 'PA', 'Z'), req('q3', 'PG', 'M')],
            'requirement_groups': [group('g1', 'PG', 'G', ['X', 'Y'])],
            'profiles': [profile('pa', 's1', 'PA'), profile('pg', 's1', 'PG')],
            'offers': [offer('om1', 'M', T1), offer('ox1', 'X', T1), offer('oy1', 'Y', T1),
                       offer('oz2', 'Z', T2)]})
        # Course-level any: Z needs M plus one of X/Y, so PA finishes in term 2.
        pa = plan.latest(self.db, 's1', 'PA')['promise']['body']['section']
        self.assertEqual(pa['term'], T2)
        terms = scheduled_terms(pa)
        self.assertEqual((terms['M'], terms['Z']), (T1, T2))
        self.assertEqual(len({'X', 'Y'} & set(terms)), 1)
        # Program-level group: PG needs only M and one of X/Y, finishing in term 1.
        pg = plan.latest(self.db, 's1', 'PG')['promise']['body']['section']
        self.assertEqual(pg['term'], T1)
        self.assertEqual(len({'X', 'Y'} & set(scheduled_terms(pg))), 1)
        self.assertNotIn('Z', scheduled_terms(pg))
        # The course-level rule itself is still evaluated by the old entry alone.
        self.assertEqual(prereq.qualify(self.db, 's1', 'Z', 2026, {'M'})['decision'],
                         'ineligible')
        self.assertEqual(prereq.qualify(self.db, 's1', 'Z', 2026, {'M', 'X'})['decision'],
                         'eligible')

    def test_co_enrollment_never_counts_as_prerequisite(self):
        self.load_catalog([course('cn', 'N', 2), course('co', 'O', 2)])
        prereq.ingest(self.db, [rule('ro', 'O', all_=['N'])])
        degree.ingest(self.db, {
            'requirements': [req('q1', 'P3', 'N'), req('q2', 'P3', 'O')],
            'profiles': [profile('p1', 's1', 'P3')],
            'offers': [offer('on1', 'N', T1), offer('oo1', 'O', T1), offer('oo2', 'O', T2)]})
        verdict = plan.latest(self.db, 's1', 'P3')['promise']['body']['section']
        self.assertEqual(verdict['term'], T2)
        self.assertEqual(scheduled_terms(verdict), {'N': T1, 'O': T2})
        cond = plan.latest(self.db, 's1', 'P3')['conditional']['body']['section']
        earlier = {e['term']: e for e in cond['infeasible_before']}
        self.assertEqual(earlier[T1]['key_constraints'], ['prereq'])

    def test_late_offer_correction_replans_dependents(self):
        self.load_catalog([course('ca', 'A', 4), course('cb', 'B', 2),
                           course('cc', 'C', 4), course('cd', 'D', 2)])
        prereq.ingest(self.db, [rule('rc', 'C', all_=['A']), rule('rd', 'D', all_=['B'])])
        batch = {'requirements': [req('q1', 'P1', 'A'), req('q2', 'P1', 'B'),
                                  req('q3', 'P1', 'C'), req('q4', 'P1', 'D')],
                 'profiles': [profile('p1', 's1', 'P1')],
                 'offers': [offer('oa1', 'A', T1), offer('ob1', 'B', T1),
                            offer('oc2', 'C', T2), offer('od2', 'D', T2)]}
        degree.ingest(self.db, batch)
        degree.ingest(self.db, {'offers': [offer('ob1-stop', 'B', T1, available=False),
                                           offer('ob2', 'B', T2), offer('od3', 'D', T3)]})
        self.assertEqual(plan.latest(self.db, 's1', 'P1')['conditional']['term'], T2)
        # A later signed version settles the dispute: B is stopped in term 1.
        result = degree.ingest(self.db, {'offers': [
            offer('ob1-v2', 'B', T1, available=False, version=2, time='2026-01-07T00:00:00Z')]})
        self.assertEqual(result['plans'][0]['conditional_term'], T3)
        latest = plan.latest(self.db, 's1', 'P1')
        self.assertEqual((latest['conditional']['term'], latest['promise']['term']), (T3, T3))
        self.assertEqual(latest['conditional']['body']['section']['assumptions'], [])
        # Every earlier conclusion is preserved, original events untouched.
        terms = [json_body['section']['term'] for json_body in
                 (json_loads(h['body']) for h in plan.history(self.db, 's1', 'P1', 'conditional'))]
        self.assertEqual(terms, [T2, T2, T3])
        bodies = {h['event_id'] for h in rows(self.db, 'SELECT event_id FROM evidence')}
        self.assertTrue({'ob1', 'ob1-stop', 'ob1-v2'} <= bodies)

    def test_equivalence_revocation_replans_graduation(self):
        self.load_catalog([course('ca', 'A', 4), course('cb', 'B', 4),
                           course('cc', 'C', 4), course('cd', 'D', 2)])
        prereq.ingest(self.db, [rule('rc', 'C', all_=['A']), rule('rd', 'D', all_=['B'])])
        credit.ingest(self.db, [transcript('ts', 's1', 'S'),
                                agreement('asb', 'S', 'B', min_credits=4)])
        degree.ingest(self.db, {
            'requirements': [req('q1', 'PR', 'A'), req('q2', 'PR', 'B'),
                             req('q3', 'PR', 'C'), req('q4', 'PR', 'D')],
            'profiles': [profile('p1', 's1', 'PR')],
            'offers': [offer('oa1', 'A', T1), offer('ob1', 'B', T1), offer('ob2', 'B', T2),
                       offer('oc2', 'C', T2), offer('oc3', 'C', T3),
                       offer('od2', 'D', T2), offer('od3', 'D', T3)]})
        # B is covered by the recognized equivalence: only A, C, D are scheduled.
        before = plan.latest(self.db, 's1', 'PR')['promise']['body']['section']
        self.assertEqual(before['term'], T2)
        self.assertNotIn('B', scheduled_terms(before))
        # Revoking the transcript forces B back into the plan and pushes term 3.
        result = credit.ingest(self.db, [transcript('ts-revoke', 's1', 'S', status='revoked',
                                                    time='2026-03-01T00:00:00Z')])
        self.assertEqual(result['plans'][0]['promise_term'], T3)
        after = plan.latest(self.db, 's1', 'PR')['promise']['body']['section']
        self.assertEqual(after['term'], T3)
        self.assertEqual(scheduled_terms(after), {'A': T1, 'B': T2, 'C': T3, 'D': T3})
        award = self.db.execute("SELECT decision, reason FROM credit_award "
                                "WHERE target_course='B'").fetchone()
        self.assertEqual((award['decision'], award['reason']),
                         ('rejected', 'transcript_revoked_or_failed'))
        causes = {h['cause'] for h in plan.history(self.db, 's1', 'PR', 'promise')}
        self.assertIn('credit', causes)

    def test_source_credit_cannot_fund_two_groups(self):
        self.load_catalog([course('cm', 'M', 2), course('cx', 'X', 2), course('cy', 'Y', 2)])
        credit.ingest(self.db, [transcript('ts', 's1', 'S', credits=2),
                                agreement('asx', 'S', 'X'),
                                agreement('asy', 'S', 'Y')])
        degree.ingest(self.db, {
            'requirements': [req('q1', 'PD', 'M')],
            'requirement_groups': [group('g1', 'PD', 'G1', ['X']),
                                   group('g2', 'PD', 'G2', ['Y'])],
            'profiles': [profile('p1', 's1', 'PD')],
            'offers': [offer('om1', 'M', T1), offer('ox1', 'X', T1), offer('oy1', 'Y', T1)]})
        awards = {r['target_course']: r for r in
                  rows(self.db, 'SELECT * FROM credit_award')}
        spent = [t for t, r in awards.items() if r['decision'] == 'rejected']
        self.assertEqual(len(spent), 1)
        self.assertEqual(awards[spent[0]]['reason'], 'source_already_spent')
        verdict = plan.latest(self.db, 's1', 'PD')['promise']['body']['section']
        self.assertEqual(verdict['term'], T1)
        # The plan must schedule the target the spent source could not also fund.
        self.assertIn(spent[0], scheduled_terms(verdict))
        self.assertNotIn([t for t in awards if t not in spent][0], scheduled_terms(verdict))

    def test_same_course_cannot_fill_two_mutually_exclusive_groups(self):
        self.load_catalog([course('cm', 'M', 2), course('cx', 'X', 2)])
        credit.ingest(self.db, [transcript('tx', 's1', 'X', credits=2)])
        degree.ingest(self.db, {
            'requirements': [req('q1', 'PE', 'M')],
            'requirement_groups': [group('g1', 'PE', 'G1', ['X']),
                                   group('g2', 'PE', 'G2', ['X'])],
            'profiles': [profile('p1', 's1', 'PE')],
            'offers': [offer('om1', 'M', T1)]})
        verdict = plan.latest(self.db, 's1', 'PE')['promise']['body']['section']
        self.assertEqual((verdict['state'], verdict['term']), ('infeasible', None))
        self.assertEqual(verdict['key_constraints'], ['equivalence'])

    def test_catalog_credit_revision_replans(self):
        self.load_catalog([course('ca', 'A', 4), course('cb', 'B', 2),
                           course('cc', 'C', 4), course('cd', 'D', 2)])
        prereq.ingest(self.db, [rule('rc', 'C', all_=['A']), rule('rd', 'D', all_=['B'])])
        degree.ingest(self.db, {
            'requirements': [req('q1', 'P1', 'A'), req('q2', 'P1', 'B'),
                             req('q3', 'P1', 'C'), req('q4', 'P1', 'D')],
            'profiles': [profile('p1', 's1', 'P1')],
            'offers': [offer('oa1', 'A', T1), offer('ob1', 'B', T1), offer('ob2', 'B', T2),
                       offer('oc2', 'C', T2), offer('oc3', 'C', T3),
                       offer('od2', 'D', T2), offer('od3', 'D', T3)]})
        self.assertEqual(plan.latest(self.db, 's1', 'P1')['promise']['term'], T2)
        # B grows from 2 to 4 credits: A+B no longer fit one term.
        result = catalog.ingest(self.db, [course('cb-v2', 'B', 4, version=2,
                                                 time='2026-02-10T00:00:00Z')])
        self.assertEqual(result['plans'][0]['promise_term'], T3)
        after = plan.latest(self.db, 's1', 'P1')['promise']['body']['section']
        self.assertEqual(after['term'], T3)
        self.assertEqual(scheduled_terms(after), {'A': T1, 'B': T2, 'C': T3, 'D': T3})
        self.assertEqual(catalog.current(self.db, 'B', 2026)['credits'], 4)
        causes = {h['cause'] for h in plan.history(self.db, 's1', 'P1', 'promise')}
        self.assertIn('catalog', causes)

    def test_prereq_rule_change_replans_graduation(self):
        self.load_catalog([course('ca', 'A', 4), course('cb', 'B', 2), course('cc', 'C', 4),
                           course('cd', 'D', 2), course('cq', 'Q', 2)])
        prereq.ingest(self.db, [rule('rc', 'C', all_=['A']), rule('rd', 'D', all_=['B'])])
        degree.ingest(self.db, {
            'requirements': [req('q1', 'P1', 'A'), req('q2', 'P1', 'B'),
                             req('q3', 'P1', 'C'), req('q4', 'P1', 'D')],
            'profiles': [profile('p1', 's1', 'P1')],
            'offers': [offer('oa1', 'A', T1), offer('ob1', 'B', T1),
                       offer('oc2', 'C', T2), offer('od2', 'D', T2), offer('od3', 'D', T3),
                       offer('oq2', 'Q', T2)]})
        self.assertEqual(plan.latest(self.db, 's1', 'P1')['promise']['term'], T2)
        # D gains an any-prerequisite on Q, only offered in term 2.
        result = prereq.ingest(self.db, [rule('rd-v2', 'D', all_=['B'], any_=['Q'],
                                              time='2026-01-08T00:00:00Z')])
        self.assertEqual(result['plans'][0]['promise_term'], T3)
        after = plan.latest(self.db, 's1', 'P1')['promise']['body']['section']
        self.assertEqual(after['term'], T3)
        self.assertEqual(scheduled_terms(after)['Q'], T2)
        causes = {h['cause'] for h in plan.history(self.db, 's1', 'P1', 'promise')}
        self.assertIn('prereq', causes)

    def test_infeasible_evidence_and_retransmission(self):
        self.load_catalog([course('cm', 'M', 2), course('cz', 'Z', 2)])
        batch = {'requirements': [req('q1', 'PN', 'M'), req('q2', 'PN', 'Z')],
                 'profiles': [profile('p1', 's1', 'PN')],
                 'offers': [offer('om1', 'M', T1)]}
        degree.ingest(self.db, batch)
        verdict = plan.latest(self.db, 's1', 'PN')
        section = verdict['promise']['body']['section']
        self.assertEqual((section['state'], section['term']), ('infeasible', None))
        self.assertEqual(section['key_constraints'], ['offering'])
        evidence = verdict['promise']['body']['evidence']
        self.assertEqual([o['course_id'] for o in evidence['offers']], ['M'])
        self.assertEqual(evidence['catalog']['Z']['2026']['decision'], 'published')
        count = self.db.execute('SELECT count(*) FROM plan_case').fetchone()[0]
        # Retransmitting the same batch imports nothing and adds no new verdict.
        self.assertEqual(degree.ingest(self.db, batch)['imported'], 0)
        plan.verify(self.db, 's1', 'PN', cause='manual')
        self.assertEqual(self.db.execute('SELECT count(*) FROM plan_case').fetchone()[0], count)

    def test_conflicting_event_id_rolls_back_whole_batch(self):
        self.load_catalog([course('ca', 'A', 4)])
        degree.ingest(self.db, {
            'requirements': [req('q1', 'P1', 'A')],
            'profiles': [profile('p1', 's1', 'P1')],
            'offers': [offer('oa1', 'A', T1)]})
        plans_before = self.db.execute('SELECT count(*) FROM plan_case').fetchone()[0]
        bad = dict(offer('oa1', 'A', T1), term=T3)  # same event id, new payload
        with self.assertRaises(ValueError):
            degree.ingest(self.db, {'offers': [offer('oa3', 'A', T3), bad]})
        self.assertIsNone(self.db.execute(
            "SELECT event_id FROM evidence WHERE event_id='oa3'").fetchone())
        self.assertEqual(self.db.execute('SELECT count(*) FROM degree_offer').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM plan_case').fetchone()[0],
                         plans_before)


def json_loads(body):
    import json
    return json.loads(body)


if __name__ == '__main__':
    unittest.main()
