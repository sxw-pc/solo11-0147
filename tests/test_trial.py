import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from curriculum import catalog, survey, survey_roster, trial
from curriculum.store import connect

COURSE, YEAR, FORM = 'C3', 2026, 'teaching'


def group_units(pattern):
    arms = {}
    for arm, tokens in pattern:
        arms.setdefault(arm, []).extend(tokens)
    return [
        {'cluster_id': f'g{n}', 'arm': arm,
         'tokens': [f't{t}' for t in range(start, start + 2)]}
        for n, (arm, start) in enumerate(
            [('A', 1), ('A', 3), ('B', 5), ('B', 7)], start=1)
    ]


def plan_event(unit='group', plan_id='p-1', plan_version=1, strata=None,
               event_time='2026-03-01T00:00:00Z', event_id='plan-1',
               alpha=0.05, arm_versions=None, arms=('A', 'B')):
    if strata is None:
        strata = [{
            'stratum_id': 'day', 'quota': {'A': 2, 'B': 2},
            'units': [
                {'cluster_id': 'g1', 'arm': 'A', 'tokens': ['t1', 't2']},
                {'cluster_id': 'g2', 'arm': 'A', 'tokens': ['t3', 't4']},
                {'cluster_id': 'g3', 'arm': 'B', 'tokens': ['t5', 't6']},
                {'cluster_id': 'g4', 'arm': 'B', 'tokens': ['t7', 't8']},
            ],
        }]
    return {
        'event_id': event_id, 'course_id': COURSE, 'year': YEAR, 'form_id': FORM,
        'plan_id': plan_id, 'plan_version': plan_version,
        'randomization_unit': unit, 'alpha': alpha, 'two_sided': True,
        'arms': list(arms),
        'arm_versions': arm_versions or {'A': 1, 'B': 2},
        'event_time': event_time,
        'strata': strata,
    }


def person_plan(plan_version=2, event_id='plan-person'):
    return plan_event(
        unit='person', plan_id='p-person', plan_version=plan_version,
        event_id=event_id,
        strata=[{
            'stratum_id': 'day', 'quota': {'A': 4, 'B': 4},
            'units': [{'cluster_id': f'u{i}', 'arm': 'A' if i < 5 else 'B',
                       'tokens': [f't{i}']} for i in range(1, 9)],
        }])


class TrialBase(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')
        catalog.ingest(self.db, json.loads(
            (ROOT / 'fixtures/curriculum/catalog_good.json').read_text(encoding='utf-8')))
        survey.ingest(self.db, {'forms': [
            {'event_id': 'form-1', 'course_id': COURSE, 'year': YEAR,
             'form_id': FORM, 'version': 1, 'question': 'wording A',
             'issuer': 'school', 'event_time': '2026-02-01T00:00:00Z'},
            {'event_id': 'form-2', 'course_id': COURSE, 'year': YEAR,
             'form_id': FORM, 'version': 2, 'question': 'wording B',
             'issuer': 'school', 'event_time': '2026-02-02T00:00:00Z'},
        ]})

    def tearDown(self):
        self.db.close()

    def invite(self, cohorts, event_id='inv', event_time='2026-03-02T00:00:00Z',
               eligible=True):
        if isinstance(cohorts, (list, tuple, set)):
            cohorts = {token: 'day' for token in cohorts}
        return survey_roster.ingest(self.db, {'invitations': [
            {'event_id': f'{event_id}-{token}', 'course_id': COURSE, 'year': YEAR,
             'form_id': FORM, 'response_token': token, 'cohort': cohort,
             'eligible': eligible, 'event_time': event_time}
            for token, cohort in cohorts.items()]})

    def deliver(self, tokens, arm, version, event_id=None, kind='initial',
                time='2026-03-03T00:00:00Z', source_token=None):
        return trial.ingest(self.db, {'deliveries': [
            {'event_id': event_id or f'del-{arm}-{token}', 'course_id': COURSE,
             'year': YEAR, 'form_id': FORM, 'response_token': token, 'arm': arm,
             'form_version': version, 'kind': kind, 'source_token': source_token,
             'event_time': time} for token in tokens]})

    def respond(self, mapping, event_id='resp', time='2026-03-05T00:00:00Z'):
        return survey.ingest(self.db, {'responses': [
            {'event_id': f'{event_id}-{token}', 'course_id': COURSE, 'year': YEAR,
             'form_id': FORM, 'response_token': token, 'score': score,
             'event_time': time} for token, score in mapping.items()]})

    def outcome(self):
        self.deliver([f't{i}' for i in range(1, 5)], 'A', 1)
        self.deliver([f't{i}' for i in range(5, 9)], 'B', 2)
        self.respond({**{f't{i}': 5 for i in range(1, 5)},
                      **{f't{i}': 1 for i in range(5, 9)}})

    def standard(self, cohort='day'):
        self.invite({f't{i}': cohort for i in range(1, 9)})
        self.outcome()


class GroupRandomizationTests(TrialBase):
    def test_group_and_person_units_give_different_tail_probabilities(self):
        trial.ingest(self.db, {'plans': [plan_event(), person_plan()]})
        self.standard()
        grouped = trial.verify(self.db, COURSE, YEAR, FORM, plan_id='p-1')['result']
        personal = trial.verify(self.db, COURSE, YEAR, FORM,
                                plan_id='p-person')['result']
        self.assertEqual((grouped['permutation']['space_size'],
                          grouped['permutation']['extreme']), (6, 2))
        self.assertAlmostEqual(grouped['permutation']['tail_probability'], 2 / 6)
        self.assertEqual(grouped['trial']['decision'], 'insufficient_evidence')
        self.assertEqual((personal['permutation']['space_size'],
                          personal['permutation']['extreme']), (70, 2))
        self.assertAlmostEqual(personal['permutation']['tail_probability'], 2 / 70)
        self.assertEqual(personal['trial']['decision'], 'wording_effect')
        self.assertEqual(grouped['intention_to_treat']['grouping'], 'assigned_arm')
        self.assertEqual(personal['intention_to_treat']['grouping'], 'assigned_arm')
        self.assertEqual(grouped['intention_to_treat']['difference'], 4)
        # only the person-level plan may sign a causal conclusion here
        self.assertFalse(grouped['causal_claim'])
        self.assertTrue(personal['causal_claim'])
        self.assertFalse(grouped['contaminations'])
        self.assertFalse(personal['contaminations'])

    def test_withdrawal_shrinks_the_legal_space_and_units(self):
        trial.ingest(self.db, {'plans': [plan_event()]})
        self.standard()
        before = trial.verify(self.db, COURSE, YEAR, FORM)['result']
        self.assertEqual(before['permutation']['space_size'], 6)

        # withdraw one whole B group: only the remaining legal layouts count
        self.invite({token: 'day' for token in ('t7', 't8')}, event_id='wd',
                    event_time='2026-03-06T00:00:00Z', eligible=False)
        after = trial.verify(self.db, COURSE, YEAR, FORM)['result']
        self.assertEqual(after['decision'], 'verified')
        self.assertEqual(after['permutation']['space_size'], 3)
        self.assertEqual(sorted({t['token'] for t in after['traces']}),
                         [f't{i}' for i in range(1, 7)])
        # the earlier verification is preserved verbatim
        history = trial.history(self.db, COURSE, YEAR, FORM)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]['result']['permutation']['space_size'], 6)
        self.assertEqual(history[0]['result']['traces'],
                         before['traces'])

    def test_withdrawal_of_every_member_removes_the_unit(self):
        trial.ingest(self.db, {'plans': [plan_event()]})
        self.standard()
        for event_id, tokens in (('wd', ('t7', 't8')), ('wd2', ('t1', 't2'))):
            self.invite({token: 'day' for token in tokens}, event_id=event_id,
                        event_time='2026-03-06T00:00:00Z', eligible=False)
        after = trial.verify(self.db, COURSE, YEAR, FORM)['result']
        # units left: g2(A), g3(B); either could be the A group -> 2 layouts
        self.assertEqual(after['permutation']['space_size'], 2)
        self.assertEqual(after['permutation']['extreme'], 2)
        self.assertEqual(after['permutation']['tail_probability'], 1)
        self.assertEqual(after['trial']['decision'], 'insufficient_evidence')

    def test_cohort_correction_moves_a_unit_to_another_stratum(self):
        strata = [
            {'stratum_id': 'day', 'quota': {'A': 1, 'B': 1}, 'units': [
                {'cluster_id': 'g1', 'arm': 'A', 'tokens': ['t1', 't2']},
                {'cluster_id': 'g2', 'arm': 'B', 'tokens': ['t3', 't4']}]},
            {'stratum_id': 'night', 'quota': {'A': 1, 'B': 1}, 'units': [
                {'cluster_id': 'g3', 'arm': 'A', 'tokens': ['t5', 't6']},
                {'cluster_id': 'g4', 'arm': 'B', 'tokens': ['t7', 't8']}]},
        ]
        trial.ingest(self.db, {'plans': [plan_event(strata=strata)]})
        self.invite({**{f't{i}': 'day' for i in (1, 2, 3, 4)},
                     **{f't{i}': 'night' for i in (5, 6, 7, 8)}})
        self.outcome()
        before = trial.verify(self.db, COURSE, YEAR, FORM)['result']
        self.assertEqual(before['permutation']['space_size'], 4)

        # correcting both members of g3 moves that whole unit to 'day'
        self.invite({token: 'day' for token in ('t5', 't6')}, event_id='fix',
                    event_time='2026-03-06T00:00:00Z')
        after = trial.verify(self.db, COURSE, YEAR, FORM)['result']
        self.assertEqual(after['decision'], 'verified')
        # 'day' now holds A,A,B and 'night' holds only B: day has C(3,2)=3
        # legal reallocations, night is fixed.  The sample grouping changed.
        self.assertEqual(after['permutation']['space_size'], 3)
        self.assertEqual({t['stratum_id'] for t in after['traces']
                          if t['token'] in ('t5', 't6')}, {'day'})
        self.assertEqual(len(trial.history(self.db, COURSE, YEAR, FORM)), 2)

    def test_partial_cohort_correction_is_a_conflict(self):
        strata = [
            {'stratum_id': 'day', 'quota': {'A': 1, 'B': 1}, 'units': [
                {'cluster_id': 'g1', 'arm': 'A', 'tokens': ['t1', 't2']},
                {'cluster_id': 'g2', 'arm': 'B', 'tokens': ['t3', 't4']}]},
            {'stratum_id': 'night', 'quota': {'A': 1, 'B': 1}, 'units': [
                {'cluster_id': 'g3', 'arm': 'A', 'tokens': ['t5', 't6']},
                {'cluster_id': 'g4', 'arm': 'B', 'tokens': ['t7', 't8']}]},
        ]
        trial.ingest(self.db, {'plans': [plan_event(strata=strata)]})
        self.invite({**{f't{i}': 'day' for i in (1, 2, 3, 4)},
                     **{f't{i}': 'night' for i in (5, 6, 7, 8)}})
        self.outcome()
        self.invite({'t7': 'day'}, event_id='fix',
                    event_time='2026-03-06T00:00:00Z')
        result = trial.verify(self.db, COURSE, YEAR, FORM)['result']
        self.assertEqual(result['decision'], 'not_interpretable')
        self.assertEqual(result['reason'], 'cohort_conflict')
        self.assertEqual(result['conflicts'][0]['cluster_id'], 'g4')
        self.assertEqual(result['conflicts'][0]['tokens'], ['t7', 't8'])
        self.assertIn('fix-t7', result['conflicts'][0]['evidence'])
        self.assertIsNone(result['permutation'])

    def test_stratum_quotas_limit_the_reallocation_space(self):
        strata = [{'stratum_id': 's1', 'quota': {'A': 1, 'B': 2}, 'units': [
            {'cluster_id': 'g1', 'arm': 'A', 'tokens': ['t1', 't2']},
            {'cluster_id': 'g2', 'arm': 'B', 'tokens': ['t3', 't4']},
            {'cluster_id': 'g3', 'arm': 'B', 'tokens': ['t5', 't6']}]}]
        trial.ingest(self.db, {'plans': [plan_event(strata=strata)]})
        self.invite({f't{i}': 's1' for i in range(1, 7)})
        self.deliver(['t1', 't2'], 'A', 1)
        self.deliver(['t3', 't4', 't5', 't6'], 'B', 2)
        self.respond({'t1': 5, 't2': 5, 't3': 1, 't4': 1, 't5': 1, 't6': 1})
        result = trial.verify(self.db, COURSE, YEAR, FORM)['result']
        # 3 choose 1 layouts, each keeping exactly one A unit
        self.assertEqual(result['permutation']['space_size'], 3)

    def test_plan_after_invitation_is_not_interpretable(self):
        self.invite([f't{i}' for i in range(1, 9)])
        late = plan_event(event_id='plan-late', plan_id='p-late',
                          event_time='2026-03-04T00:00:00Z')
        trial.ingest(self.db, {'plans': [late]})
        self.deliver([f't{i}' for i in range(1, 5)], 'A', 1)
        self.deliver([f't{i}' for i in range(5, 9)], 'B', 2)
        self.respond({**{f't{i}': 5 for i in range(1, 5)},
                      **{f't{i}': 1 for i in range(5, 9)}})
        result = trial.verify(self.db, COURSE, YEAR, FORM, plan_id='p-late')['result']
        self.assertEqual(result['decision'], 'not_interpretable')
        self.assertEqual(result['reason'], 'plan_after_invitation')
        self.assertEqual(result['conflicts'][0]['earliest_invitation_event'],
                         'inv-t1')
        self.assertIsNone(result['permutation'])
        self.assertFalse(result['causal_claim'])

    def test_backdated_invitation_invalidates_precedence(self):
        trial.ingest(self.db, {'plans': [plan_event()]})
        # an invitation ledgered later but dated before the plan
        self.invite([f't{i}' for i in range(1, 9)], event_id='backdated',
                    event_time='2026-02-28T00:00:00Z')
        result = trial.verify(self.db, COURSE, YEAR, FORM)['result']
        self.assertEqual(result['decision'], 'not_interpretable')
        self.assertEqual(result['reason'], 'plan_after_invitation')
        self.assertEqual(result['conflicts'][0]['earliest_invitation_event'],
                         'backdated-t1')

    def test_plan_registered_before_invitation_passes_precedence(self):
        trial.ingest(self.db, {'plans': [plan_event()]})
        self.standard()
        result = trial.verify(self.db, COURSE, YEAR, FORM)['result']
        self.assertEqual(result['decision'], 'verified')
        self.assertEqual(trial.plan_for(self.db, COURSE, YEAR, FORM)[
            'precedence_ok'], 1)


class DeliveryTests(TrialBase):
    def test_wrong_version_keeps_itt_but_marks_causal_claim(self):
        trial.ingest(self.db, {'plans': [plan_event()]})
        self.invite([f't{i}' for i in range(1, 9)])
        # g1 was assigned A but received B's form; the rest are as assigned
        self.deliver(['t1', 't2'], 'B', 2)
        self.deliver(['t3', 't4'], 'A', 1)
        self.deliver([f't{i}' for i in range(5, 9)], 'B', 2)
        self.respond({**{f't{i}': 5 for i in range(1, 5)},
                      **{f't{i}': 1 for i in range(5, 9)}})
        result = trial.verify(self.db, COURSE, YEAR, FORM)['result']
        self.assertEqual(result['decision'], 'verified')
        # ITT keeps g1's four answers in arm A
        self.assertEqual(result['intention_to_treat']['A'],
                         {'mean_score': 5.0, 'responses': 4})
        self.assertEqual(result['descriptive']['B']['responses'], 6)
        self.assertAlmostEqual(result['descriptive']['B']['mean_score'],
                               14 / 6)
        self.assertEqual(result['descriptive']['A']['mean_score'], 5.0)
        self.assertFalse(result['causal_claim'])
        kinds = {item['kind'] for item in result['contaminations']}
        self.assertIn('cross_arm_delivery', kinds)
        # permutation still follows the registered allocation
        self.assertEqual(result['permutation']['space_size'], 6)

    def test_resent_wrong_arm_is_traced_once_per_token(self):
        trial.ingest(self.db, {'plans': [plan_event()]})
        self.standard()
        self.deliver(['t1'], 'B', 2, event_id='resend-t1', kind='resend',
                     time='2026-03-04T00:00:00Z')
        result = trial.verify(self.db, COURSE, YEAR, FORM)['result']
        self.assertEqual(result['intention_to_treat']['A']['responses'], 4)
        self.assertEqual(len({t['token'] for t in result['traces']}), 8)
        trace = next(t for t in result['traces'] if t['token'] == 't1')
        self.assertEqual((trace['assigned_arm'], trace['delivered_arm']), ('A', 'B'))
        self.assertEqual(len(trace['delivery_events']), 2)
        kinds = {item['kind'] for item in result['contaminations']}
        self.assertIn('conflicting_delivery_history', kinds)
        self.assertFalse(result['causal_claim'])

    def test_cross_group_forward_does_not_override_assignment_order(self):
        trial.ingest(self.db, {'plans': [plan_event()]})
        self.standard()
        trial.ingest(self.db, {'deliveries': [{
            'event_id': 'forward-t3', 'course_id': COURSE, 'year': YEAR,
            'form_id': FORM, 'response_token': 't3', 'arm': 'B',
            'form_version': 2, 'kind': 'forward', 'source_token': 't5',
            'event_time': '2026-03-04T00:00:00Z'}]})
        result = trial.verify(self.db, COURSE, YEAR, FORM)['result']
        kinds = {item['kind'] for item in result['contaminations']}
        self.assertIn('cross_group_forward', kinds)
        self.assertIn('cross_arm_delivery', kinds)
        # t3 is counted once and stays in its assigned A group for ITT
        self.assertEqual(result['intention_to_treat']['A']['responses'], 4)
        self.assertFalse(result['causal_claim'])
        trace = next(t for t in result['traces'] if t['token'] == 't3')
        self.assertEqual(trace['forwarded_from'], 't5')

    def test_late_correction_changes_contrast_not_assignment(self):
        trial.ingest(self.db, {'plans': [plan_event()]})
        self.standard()
        before = trial.verify(self.db, COURSE, YEAR, FORM)['result']
        self.assertEqual(before['permutation']['statistic'], 4)
        self.respond({'t5': 5}, event_id='correction',
                     time='2026-03-09T00:00:00Z')
        after = trial.verify(self.db, COURSE, YEAR, FORM)['result']
        self.assertLess(after['permutation']['statistic'], 4)
        self.assertEqual(after['intention_to_treat']['B']['responses'], 4)
        # the correction does not touch the registered plan or old verification
        plan = trial.plan_for(self.db, COURSE, YEAR, FORM)
        self.assertEqual(plan['plan_id'], 'p-1')
        self.assertEqual(before['intention_to_treat']['B']['mean_score'], 1)
        self.assertEqual(trial.history(self.db, COURSE, YEAR, FORM)[0]['result']
                         ['permutation']['statistic'], 4)

    def test_versions_are_kept_and_conflicts_rejected(self):
        trial.ingest(self.db, {'plans': [plan_event()]})
        trial.ingest(self.db, {'plans': [plan_event(
            plan_version=2, event_time='2026-03-01T01:00:00Z', event_id='plan-2',
            arm_versions={'A': 2, 'B': 2})]})
        self.assertEqual([p['plan_version'] for p in trial.plans(
            self.db, COURSE, YEAR, FORM)], [1, 2])
        with self.assertRaises(ValueError):
            trial.ingest(self.db, {'plans': [plan_event(
                plan_version=2, event_time='2026-03-01T02:00:00Z',
                event_id='plan-2-alt', arm_versions={'A': 3, 'B': 3})]})
        with self.assertRaises(ValueError):
            trial.plan_for(self.db, COURSE, YEAR, FORM)  # ambiguous
        self.standard()
        old = trial.verify(self.db, COURSE, YEAR, FORM, plan_version=1)['result']
        new = trial.verify(self.db, COURSE, YEAR, FORM, plan_version=2)['result']
        self.assertEqual(old['arm_versions'], {'A': 1, 'B': 2})
        self.assertEqual(new['arm_versions'], {'A': 2, 'B': 2})
        self.assertIn('wrong_version_delivery',
                      {c['kind'] for c in new['contaminations']})
        self.assertEqual({h['plan_version'] for h in trial.history(
            self.db, COURSE, YEAR, FORM)}, {1, 2})

    def test_unplanned_eligible_token_is_minimal_conflict_evidence(self):
        trial.ingest(self.db, {'plans': [plan_event()]})
        self.standard()
        self.invite(['t9'], event_id='extra')
        result = trial.verify(self.db, COURSE, YEAR, FORM)['result']
        self.assertEqual(result['decision'], 'not_interpretable')
        self.assertEqual(result['reason'], 'unplanned_token')
        self.assertEqual(result['conflicts'][0]['tokens'], ['t9'])
        self.assertEqual(result['conflicts'][0]['evidence'], ['extra-t9'])

    def test_planned_token_missing_from_roster_is_a_conflict(self):
        trial.ingest(self.db, {'plans': [plan_event()]})
        self.invite([f't{i}' for i in range(1, 8)])
        result = trial.verify(self.db, COURSE, YEAR, FORM)['result']
        self.assertEqual(result['decision'], 'not_interpretable')
        self.assertEqual(result['reason'], 'uninvited_token')
        self.assertEqual(result['conflicts'][-1]['tokens'], ['t8'])

    def test_batch_rollback_on_conflicting_event_id(self):
        trial.ingest(self.db, {'plans': [plan_event()]})
        good = {'event_id': 'd-new', 'course_id': COURSE, 'year': YEAR,
                'form_id': FORM, 'response_token': 't1', 'arm': 'A',
                'form_version': 1, 'event_time': '2026-03-03T00:00:00Z'}
        bad = dict(good, response_token='t2')
        with self.assertRaises(ValueError):
            trial.ingest(self.db, {'deliveries': [good, bad]})
        self.assertIsNone(self.db.execute(
            "SELECT event_id FROM evidence WHERE event_id='d-new'").fetchone())
        self.assertEqual(self.db.execute(
            'SELECT COUNT(*) FROM trial_delivery').fetchone()[0], 0)

    def test_fixture_plan_registers_and_verifies(self):
        batch = json.loads((ROOT / 'fixtures/curriculum/trial_good.json')
                           .read_text(encoding='utf-8'))
        trial.ingest(self.db, batch)
        self.standard()
        result = trial.verify(self.db, COURSE, YEAR, FORM)['result']
        self.assertEqual((result['permutation']['space_size'],
                          result['permutation']['extreme']), (6, 2))
        self.assertEqual(result['trial']['decision'], 'insufficient_evidence')

    def test_duplicate_plan_token_is_rejected(self):
        duplicate = plan_event()
        duplicate['strata'][0]['units'][1]['tokens'] = ['t2', 't4']
        with self.assertRaises(ValueError):
            trial.ingest(self.db, {'plans': [duplicate]})
        self.assertEqual(self.db.execute(
            'SELECT COUNT(*) FROM trial_plan').fetchone()[0], 0)

    def test_independent_results_survive_trial_verification(self):
        trial.ingest(self.db, {'plans': [plan_event()]})
        self.standard()
        trial.verify(self.db, COURSE, YEAR, FORM)
        mean = survey.decide(self.db, COURSE, YEAR, FORM)
        roster = survey_roster.decide(self.db, COURSE, YEAR, FORM)
        self.assertEqual((mean['responses'], mean['mean_score']), (8, 3))
        self.assertEqual((roster['invited'], roster['responded'],
                          roster['rate']), (8, 8, 1.0))


if __name__ == '__main__':
    unittest.main()
