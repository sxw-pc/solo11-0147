"""Pre-registered wording trial: assignment, delivery and verification.

The survey mean score (``survey.decide``) and the invitation response rate
(``survey_roster.decide``) remain independent results.  This module only
answers the trial question: given the plan registered *before* invitations
went out, is the wording effect still interpretable after wrong-version
deliveries, resends, forwards, late corrections, invitation withdrawals and
cohort corrections?

Rules enforced here:

* a plan is immutable once registered; later roster or response corrections
  never rewrite it, and every verification is kept in its own history row;
* the randomization unit is either a person or a whole group, and the
  permutation space is the plan's stratum-wise reallocation space -- members
  of one group always move together;
* intention-to-treat groups by pre-registered assignment, while the
  descriptive view groups by actual delivery; the two are reported side by
  side and neither overwrites the other;
* a token contributes at most once per trial; forwards between groups or
  strata, wrong-version deliveries, resends and corrections are traced to the
  original assignment instead of silently overriding it with a later event;
* a plan that conflicts with the roster is ``not_interpretable`` and names the
  minimal conflicting evidence instead of emitting a significance value.
"""
import itertools
import json
from collections import Counter, defaultdict

from .sources import record
from .store import rows
from . import survey

SCHEMA_VERSION = 1
ARMS = ('A', 'B')


def _arm_versions(value, arms):
    if not isinstance(value, dict) or set(value) != set(arms):
        raise ValueError('arm_versions must name both arms')
    versions = {}
    for arm in arms:
        if not isinstance(value[arm], int) or value[arm] < 1:
            raise ValueError('arm version must be a positive integer')
        versions[arm] = value[arm]
    return versions


def _normalize_plan(event):
    """Validate a plan and return its canonical, immutable body."""
    if event.get('schema_version', SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ValueError('unsupported trial plan schema')
    for key in ('course_id', 'year', 'form_id', 'plan_id', 'plan_version',
                'randomization_unit', 'alpha', 'arms', 'arm_versions', 'strata'):
        if key not in event:
            raise ValueError(f'trial plan missing {key}')
    if not isinstance(event['plan_version'], int) or event['plan_version'] < 1:
        raise ValueError('plan_version must be a positive integer')
    unit = event['randomization_unit']
    if unit not in ('person', 'group'):
        raise ValueError('randomization_unit must be person or group')
    alpha = float(event['alpha'])
    if not 0 < alpha < 1:
        raise ValueError('alpha outside 0..1')
    arms = list(event['arms'])
    if len(arms) != 2 or len(set(arms)) != 2 or set(arms) != set(ARMS):
        raise ValueError('trial arms must be exactly A and B')
    versions = _arm_versions(event['arm_versions'], arms)
    strata = event['strata']
    if not strata:
        raise ValueError('trial plan needs strata')
    seen_strata, seen_clusters, seen_tokens = set(), set(), set()
    normalized_strata = []
    for stratum in strata:
        stratum_id = stratum.get('stratum_id')
        if not stratum_id or stratum_id in seen_strata:
            raise ValueError('stratum ids must be present and unique')
        seen_strata.add(stratum_id)
        stratum_versions = dict(versions)
        if 'arm_versions' in stratum:
            stratum_versions = _arm_versions(stratum['arm_versions'], arms)
        quota = stratum.get('quota') or {}
        if set(quota) != set(arms):
            raise ValueError('stratum quota must cover both arms')
        quota = {arm: int(quota[arm]) for arm in arms}
        if any(quota[arm] < 1 for arm in arms):
            raise ValueError(f'stratum {stratum_id} needs both arms')
        units = []
        for cluster in stratum.get('units', []):
            cluster_id = cluster.get('cluster_id')
            arm = cluster.get('arm')
            tokens = list(cluster.get('tokens') or [])
            if not cluster_id or cluster_id in seen_clusters:
                raise ValueError('cluster ids must be present and unique')
            if arm not in arms:
                raise ValueError(f'cluster {cluster_id} needs an assigned arm')
            if not tokens:
                raise ValueError(f'cluster {cluster_id} needs tokens')
            if len(tokens) != len(set(tokens)):
                raise ValueError(f'duplicate token inside cluster {cluster_id}')
            repeated = seen_tokens.intersection(tokens)
            if repeated:
                raise ValueError(f'token assigned twice: {sorted(repeated)[0]}')
            seen_clusters.add(cluster_id)
            seen_tokens.update(tokens)
            units.append({'cluster_id': cluster_id, 'arm': arm, 'tokens': tokens})
        counted = Counter(item['arm'] for item in units)
        if any(counted[arm] != quota[arm] for arm in arms):
            raise ValueError(f'quota in stratum {stratum_id} does not match its units')
        if sum(quota.values()) != len(units):
            raise ValueError(f'quota in stratum {stratum_id} does not match its units')
        normalized_strata.append({
            'stratum_id': stratum_id,
            'quota': quota,
            'arm_versions': stratum_versions,
            'units': units,
        })
    return {
        'schema_version': SCHEMA_VERSION,
        'course_id': event['course_id'], 'year': event['year'],
        'form_id': event['form_id'],
        'randomization_unit': unit,
        'alpha': alpha,
        'two_sided': bool(event.get('two_sided', True)),
        'arms': arms,
        'arm_versions': versions,
        'strata': normalized_strata,
    }


def ingest(db, data):
    """Register plans and record delivery receipts atomically."""
    imported = 0
    with db:
        for event in data.get('plans', []):
            body = _normalize_plan(event)
            existing = db.execute(
                'SELECT * FROM trial_plan WHERE course_id=? AND year=? AND form_id=? '
                'AND plan_version=?',
                (event['course_id'], event['year'], event['form_id'],
                 event['plan_version'])).fetchone()
            if existing:
                if (existing['plan_id'] != event['plan_id']
                        or json.loads(existing['plan_json']) != body):
                    raise ValueError('conflicting trial plan version')
                continue
            payload = dict(body, plan_id=event['plan_id'], plan_version=event['plan_version'])
            payload.update(course_id=event['course_id'], year=event['year'],
                           form_id=event['form_id'], event_id=event['event_id'],
                           event_time=event['event_time'])
            if not record(db, payload, 'trial-plan', event['plan_id']):
                continue
            earliest = db.execute(
                'SELECT e.event_id,e.event_time FROM survey_invitation i '
                'JOIN evidence e USING(event_id) WHERE i.course_id=? AND i.year=? '
                'AND i.form_id=? ORDER BY e.event_time,e.event_id LIMIT 1',
                (event['course_id'], event['year'], event['form_id'])).fetchone()
            precedence_ok = earliest is None or event['event_time'] < earliest['event_time']
            db.execute(
                'INSERT INTO trial_plan VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (event['event_id'], event['course_id'], event['year'],
                 event['form_id'], event['plan_id'], event['plan_version'],
                 body['randomization_unit'], body['alpha'], int(body['two_sided']),
                 event['event_time'], int(precedence_ok),
                 earliest['event_id'] if earliest else None,
                 json.dumps(body, ensure_ascii=False, sort_keys=True)))
            imported += 1
        for receipt in data.get('deliveries', []):
            _check_receipt(receipt)
            if record(db, receipt, 'trial-delivery', receipt['response_token']):
                db.execute(
                    'INSERT INTO trial_delivery VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (receipt['event_id'], receipt['course_id'], receipt['year'],
                     receipt['form_id'], receipt['response_token'], receipt['arm'],
                     receipt.get('form_version'), receipt.get('kind', 'initial'),
                     receipt.get('source_token'), receipt.get('source_event'),
                     receipt['event_time']))
                imported += 1
    return {'imported': imported}


def _check_receipt(receipt):
    for key in ('event_id', 'course_id', 'year', 'form_id', 'response_token',
                'arm', 'event_time'):
        if key not in receipt:
            raise ValueError(f'delivery receipt missing {key}')
    if receipt['arm'] not in ARMS:
        raise ValueError('delivery arm must be A or B')
    if receipt.get('kind', 'initial') not in ('initial', 'resend', 'forward'):
        raise ValueError('delivery kind must be initial, resend or forward')
    if receipt.get('form_version') is not None and receipt['form_version'] < 1:
        raise ValueError('delivery form_version must be positive')


def plans(db, course_id, year, form_id):
    return rows(db, 'SELECT * FROM trial_plan WHERE course_id=? AND year=? AND form_id=? '
                    'ORDER BY plan_version,event_id', (course_id, year, form_id))


def plan_for(db, course_id, year, form_id, plan_id=None, plan_version=None):
    sql = 'SELECT * FROM trial_plan WHERE course_id=? AND year=? AND form_id=?'
    args = [course_id, year, form_id]
    if plan_id is not None:
        sql += ' AND plan_id=?'
        args.append(plan_id)
    if plan_version is not None:
        sql += ' AND plan_version=?'
        args.append(plan_version)
    found = rows(db, sql + ' ORDER BY plan_version,event_id', args)
    if not found:
        raise ValueError('unknown trial plan')
    if len(found) > 1:
        raise ValueError('ambiguous trial plan; specify plan_id or plan_version')
    return found[0]


def _latest_invitations(db, key):
    invitations = rows(
        db, 'SELECT i.*,e.event_time,e.event_id AS source_event FROM survey_invitation i '
            'JOIN evidence e USING(event_id) WHERE i.course_id=? AND i.year=? '
            'AND i.form_id=? ORDER BY e.event_time DESC,e.event_id DESC', key)
    latest = {}
    for item in invitations:
        latest.setdefault(item['response_token'], item)
    return latest, invitations


def _latest_responses(db, key):
    records = rows(
        db, 'SELECT r.*,e.event_time,e.event_id AS source_event FROM survey_response r '
            'JOIN evidence e USING(event_id) WHERE r.course_id=? AND r.year=? '
            'AND r.form_id=? ORDER BY e.event_time DESC,e.event_id DESC', key)
    latest = {}
    for item in records:
        latest.setdefault(item['response_token'], item)
    return latest, records


def _form_history(db, key):
    return rows(
        db, 'SELECT f.*,e.event_time FROM survey_form f JOIN evidence e USING(event_id) '
            'WHERE f.course_id=? AND f.year=? AND f.form_id=? '
            'ORDER BY e.event_time,e.event_id', key)


def _version_at(forms, version):
    return next((item for item in forms if item['version'] == version), None)


def _delivery_history(db, key):
    records = rows(
        db, 'SELECT d.*,e.event_time FROM trial_delivery d JOIN evidence e USING(event_id) '
            'WHERE d.course_id=? AND d.year=? AND d.form_id=? '
            'ORDER BY e.event_time,e.event_id', key)
    history = defaultdict(list)
    for item in records:
        history[item['response_token']].append(item)
    return history, records


def _current_units(body, invitations):
    """Group the planned units by the roster's *current* cohort.

    Withdrawals shrink a unit to its still-eligible tokens (and drop the unit
    when none remain); a cohort correction moves a whole unit to the stratum
    named by its corrected cohort.  A unit split across cohorts cannot be
    reallocated as a block, so it is a hard plan roster conflict.
    """
    units = {}
    conflicts = []
    uninvited = []
    for stratum in body['strata']:
        sid = stratum['stratum_id']
        for cluster in stratum['units']:
            eligible = []
            cohorts = set()
            evidence = []
            for token in cluster['tokens']:
                invitation = invitations.get(token)
                if invitation is None:
                    uninvited.append(token)
                    continue
                evidence.append(invitation['source_event'])
                if invitation['eligible']:
                    eligible.append(token)
                    cohorts.add(invitation['cohort'])
            if not eligible:
                continue
            if len(cohorts) > 1:
                conflicts.append({
                    'kind': 'cohort_conflict',
                    'cluster_id': cluster['cluster_id'],
                    'tokens': sorted(eligible),
                    'cohorts': sorted(cohorts),
                    'evidence': sorted(evidence),
                })
                continue
            cohort = next(iter(cohorts))
            if cohort not in {s['stratum_id'] for s in body['strata']}:
                conflicts.append({
                    'kind': 'unknown_cohort',
                    'cluster_id': cluster['cluster_id'],
                    'cohort': cohort,
                    'tokens': sorted(eligible),
                    'evidence': sorted(evidence),
                })
                continue
            units.setdefault(cohort, []).append({
                'cluster_id': cluster['cluster_id'],
                'arm': cluster['arm'],
                'tokens': eligible,
                'registered_stratum': sid,
            })
    return units, conflicts, sorted(set(uninvited))


def _layouts(units_by_stratum, scores, arms):
    """Exact permutation distribution over the plan's legal layouts.

    For each stratum we keep the registered arm counts among the units that
    are still observed (a withdrawal conditions on the remaining units), then
    enumerate every reallocation of whole units to the two arms.  Counts are
    aggregated to (sum of A scores, number of A scores) so the space can be
    large without materializing every layout.
    """
    distributions = []
    sizes = {}
    base_sum = base_count = 0
    for stratum_id in sorted(units_by_stratum):
        units = units_by_stratum[stratum_id]
        size_a = sum(1 for item in units if item['arm'] == arms[0])
        if size_a >= len(units):
            # every remaining unit is registered to A: this stratum is fixed
            for item in units:
                for token in item['tokens']:
                    if token in scores:
                        base_sum += scores[token]
                        base_count += 1
            sizes[stratum_id] = 1
            distributions.append(Counter({(0, 0): 1}))
            continue
        if size_a < 1:
            sizes[stratum_id] = 1
            distributions.append(Counter({(0, 0): 1}))
            continue
        counter = Counter()
        for chosen in itertools.combinations(range(len(units)), size_a):
            selected = set(chosen)
            score_sum = score_count = 0
            for index, item in enumerate(units):
                if index in selected:
                    for token in item['tokens']:
                        if token in scores:
                            score_sum += scores[token]
                            score_count += 1
            counter[(score_sum, score_count)] += 1
        sizes[stratum_id] = sum(counter.values())
        distributions.append(counter)
    combined = Counter({(base_sum, base_count): 1})
    for counter in distributions:
        merged = Counter()
        for (left_sum, left_count), left_ways in combined.items():
            for (right_sum, right_count), right_ways in counter.items():
                merged[(left_sum + right_sum, left_count + right_count)] += (
                    left_ways * right_ways)
        combined = merged
    return combined, sizes


def _statistic(sum_a, count_a, total_sum, total_count):
    if not count_a or count_a == total_count or not total_count:
        return None
    mean_a = sum_a / count_a
    mean_b = (total_sum - sum_a) / (total_count - count_a)
    return mean_a - mean_b


def _permutation(units_by_stratum, scores, body):
    arms = body['arms']
    combined, sizes = _layouts(units_by_stratum, scores, arms)
    total_sum = sum(scores.values())
    total_count = len(scores)
    observed = _statistic(
        sum(scores[t] for units in units_by_stratum.values() for u in units
            for t in u['tokens'] if u['arm'] == arms[0] and t in scores),
        sum(1 for units in units_by_stratum.values() for u in units
            for t in u['tokens'] if u['arm'] == arms[0] and t in scores),
        total_sum, total_count)
    if observed is None:
        return None, sizes
    threshold = abs(observed) - 1e-12 if body['two_sided'] else observed - 1e-12
    extreme = 0
    for (sum_a, count_a), ways in combined.items():
        statistic = _statistic(sum_a, count_a, total_sum, total_count)
        if statistic is None:
            continue
        if ((abs(statistic) >= threshold) if body['two_sided']
                else (statistic >= threshold)):
            extreme += ways
    space = sum(combined.values())
    return {'statistic': observed, 'space_size': space, 'extreme': extreme,
            'tail_probability': extreme / space, 'stratum_sizes': sizes}, sizes


def _means(arm_scores, arms):
    means = {arm: (sum(values) / len(values) if values else None)
             for arm, values in arm_scores.items()}
    result = {arm: {'mean_score': means.get(arm), 'responses': len(arm_scores.get(arm, []))}
              for arm in arms}
    if means.get(arms[0]) is not None and means.get(arms[1]) is not None:
        result['difference'] = means[arms[0]] - means[arms[1]]
    else:
        result['difference'] = None
    return result


def _analyze(db, body, units_by_stratum, responses, deliveries, forms):
    """Build traces, the two independent group views and contamination flags."""
    current_form = survey.current_form(
        db, body['course_id'], body['year'], body['form_id'])
    unit_of = {}
    stratum_of = {}
    for sid, units in units_by_stratum.items():
        for unit in units:
            for token in unit['tokens']:
                unit_of[token] = unit
                stratum_of[token] = sid
    traces = []
    contaminations = []
    itt_scores = {arm: [] for arm in body['arms']}
    descriptive_scores = {arm: [] for arm in body['arms']}
    per_protocol_scores = {arm: [] for arm in body['arms']}
    for token in sorted(responses):
        response = responses[token]
        unit = unit_of.get(token)
        if unit is None:
            continue
        stratum_id = stratum_of[token]
        versions = next(s['arm_versions'] for s in body['strata']
                        if s['stratum_id'] == stratum_id)
        events = deliveries.get(token, [])
        current = events[-1] if events else None
        # The form actually filled in is the one named by the delivery that
        # reached this token; current_form records today's published version so
        # that a late form revision is visible without rewriting the trace.
        answered = _version_at(forms, current['form_version']) if current else None
        current_form = survey.current_form(
            db, response['course_id'], response['year'], response['form_id'])
        trace = {
            'token': token,
            'cluster_id': unit['cluster_id'],
            'stratum_id': stratum_id,
            'assigned_arm': unit['arm'],
            'assigned_form_version': versions[unit['arm']],
            'delivered_arm': current['arm'] if current else None,
            'delivery_kind': current['kind'] if current else None,
            'delivered_form_version': current['form_version'] if current else None,
            'delivery_events': [item['event_id'] for item in events],
            'forwarded_from': current['source_token'] if current else None,
            'answered_form_version': answered['version'] if answered else None,
            'answered_form_event': answered['event_id'] if answered else None,
            'current_form_version': current_form.get('version'),
            'score': response['score'],
            'score_event': response['source_event'],
            'score_time': response['event_time'],
        }
        traces.append(trace)
        itt_scores[unit['arm']].append(response['score'])

        if current is None:
            contaminations.append({'kind': 'missing_delivery', 'token': token,
                                   'cluster_id': unit['cluster_id'],
                                   'evidence': [response['source_event']]})
        else:
            descriptive_scores[current['arm']].append(response['score'])
            if current['arm'] != unit['arm']:
                contaminations.append({
                    'kind': 'cross_arm_delivery', 'token': token,
                    'cluster_id': unit['cluster_id'],
                    'assigned_arm': unit['arm'], 'delivered_arm': current['arm'],
                    'delivery_kind': current['kind'],
                    'evidence': [current['event_id'], response['source_event']],
                })
            designated = versions[current['arm']]
            if (current['form_version'] is not None
                    and current['form_version'] != designated):
                contaminations.append({
                    'kind': 'wrong_version_delivery', 'token': token,
                    'delivered_arm': current['arm'],
                    'designated_version': designated,
                    'delivered_version': current['form_version'],
                    'evidence': [current['event_id']],
                })
            if current['form_version'] is not None and answered is None:
                contaminations.append({
                    'kind': 'unregistered_form_version', 'token': token,
                    'delivered_version': current['form_version'],
                    'evidence': [current['event_id']],
                })
            if current['kind'] == 'forward':
                source = unit_of.get(current['source_token'])
                if source is None:
                    contaminations.append({
                        'kind': 'unverified_forward', 'token': token,
                        'source_token': current['source_token'],
                        'evidence': [current['event_id']],
                    })
                elif source['cluster_id'] != unit['cluster_id']:
                    contaminations.append({
                        'kind': 'cross_group_forward', 'token': token,
                        'source_token': current['source_token'],
                        'recipient_cluster': unit['cluster_id'],
                        'source_cluster': source['cluster_id'],
                        'evidence': [current['event_id']],
                    })
            if len({item['arm'] for item in events}) > 1:
                contaminations.append({
                    'kind': 'conflicting_delivery_history', 'token': token,
                    'arms': sorted({item['arm'] for item in events}),
                    'evidence': [item['event_id'] for item in events],
                })
            clean = (current['arm'] == unit['arm']
                     and (current['form_version'] is None
                          or current['form_version'] == versions[current['arm']]))
            if clean:
                per_protocol_scores[unit['arm']].append(response['score'])
    intention = _means(itt_scores, body['arms'])
    intention['grouping'] = 'assigned_arm'
    descriptive = _means(descriptive_scores, body['arms'])
    descriptive['grouping'] = 'delivered_arm'
    per_protocol = _means(per_protocol_scores, body['arms'])
    per_protocol['grouping'] = 'delivered_arm'
    return traces, contaminations, intention, descriptive, per_protocol


def _sources(db, plan_row):
    key = (plan_row['course_id'], plan_row['year'], plan_row['form_id'])
    return {
        'plan_event': plan_row['event_id'],
        'invitations': sorted({item['event_id'] for item in rows(
            db, 'SELECT event_id FROM survey_invitation WHERE course_id=? AND year=? '
                'AND form_id=?', key)}),
        'deliveries': sorted({item['event_id'] for item in rows(
            db, 'SELECT event_id FROM trial_delivery WHERE course_id=? AND year=? '
                'AND form_id=?', key)}),
        'responses': sorted({item['event_id'] for item in rows(
            db, 'SELECT event_id FROM survey_response WHERE course_id=? AND year=? '
                'AND form_id=?', key)}),
        'forms': sorted({item['event_id'] for item in rows(
            db, 'SELECT event_id FROM survey_form WHERE course_id=? AND year=? '
                'AND form_id=?', key)}),
    }


def _save(db, plan_row, result):
    sources = _sources(db, plan_row)
    with db:
        cursor = db.execute(
            'INSERT INTO trial_verification(course_id,year,form_id,plan_id,plan_version,'
            'plan_event,decision,result_json,sources_json) VALUES (?,?,?,?,?,?,?,?,?)',
            (plan_row['course_id'], plan_row['year'], plan_row['form_id'],
             plan_row['plan_id'], plan_row['plan_version'], plan_row['event_id'],
             result['decision'], json.dumps(result, ensure_ascii=False, sort_keys=True),
             json.dumps(sources, ensure_ascii=False, sort_keys=True)))
        verification_id = cursor.lastrowid
    return {'verification_id': verification_id,
            'plan_id': plan_row['plan_id'],
            'plan_version': plan_row['plan_version'],
            'decision': result['decision'],
            'result': result, 'sources': sources}


def verify(db, course_id, year, form_id, plan_id=None, plan_version=None):
    """Verify one immutable plan version and append a history row."""
    plan_row = plan_for(db, course_id, year, form_id, plan_id, plan_version)
    body = json.loads(plan_row['plan_json'])
    key = (course_id, year, form_id)
    invitations, _ = _latest_invitations(db, key)
    responses, _ = _latest_responses(db, key)
    deliveries, _ = _delivery_history(db, key)
    forms = _form_history(db, key)
    planned_tokens = {token for stratum in body['strata']
                      for cluster in stratum['units'] for token in cluster['tokens']}

    conflicts = []
    # Re-check precedence against the invitation ledger as it stands now, so a
    # backdated invitation cannot slip in behind a plan registered earlier.
    earliest = db.execute(
        'SELECT e.event_id,e.event_time FROM survey_invitation i '
        'JOIN evidence e USING(event_id) WHERE i.course_id=? AND i.year=? '
        'AND i.form_id=? ORDER BY e.event_time,e.event_id LIMIT 1',
        key).fetchone()
    plan_before_roster = plan_row['event_time'] < earliest['event_time'] if earliest else True
    if not plan_row['precedence_ok'] or not plan_before_roster:
        conflicts.append({
            'kind': 'plan_after_invitation',
            'plan_event': plan_row['event_id'],
            'plan_time': plan_row['event_time'],
            'earliest_invitation_event': (earliest['event_id'] if earliest
                                          else plan_row['earliest_invitation_event']),
        })
    unplanned = sorted(token for token, item in invitations.items()
                       if item['eligible'] and token not in planned_tokens)
    if unplanned:
        conflicts.append({
            'kind': 'unplanned_token', 'tokens': unplanned,
            'evidence': [invitations[token]['source_event'] for token in unplanned],
        })
    units_by_stratum, unit_conflicts, uninvited = _current_units(body, invitations)
    conflicts.extend(unit_conflicts)
    if uninvited:
        conflicts.append({'kind': 'uninvited_token', 'tokens': uninvited})

    eligible_responses = {token: item for token, item in responses.items()
                          if token in planned_tokens
                          and invitations.get(token, {}).get('eligible')}
    scores = {token: item['score'] for token, item in eligible_responses.items()}
    result = {
        'plan_id': plan_row['plan_id'],
        'plan_version': plan_row['plan_version'],
        'randomization_unit': body['randomization_unit'],
        'alpha': body['alpha'], 'two_sided': body['two_sided'],
        'arm_versions': body['arm_versions'],
        'conflicts': conflicts,
        'contaminations': [],
        'traces': [], 'intention_to_treat': None, 'descriptive': None,
        'per_protocol': None, 'permutation': None, 'trial': None,
        'causal_claim': False,
    }
    if conflicts:
        result.update(decision='not_interpretable', reason=conflicts[0]['kind'])
        return _save(db, plan_row, result)

    traces, contaminations, intention, descriptive, per_protocol = _analyze(
        db, body, units_by_stratum, eligible_responses, deliveries, forms)
    result.update(traces=traces, contaminations=contaminations,
                  intention_to_treat=intention, descriptive=descriptive,
                  per_protocol=per_protocol)
    if not scores:
        result.update(decision='not_interpretable', reason='no_responses')
        return _save(db, plan_row, result)

    permutation, sizes = _permutation(units_by_stratum, scores, body)
    if permutation is None:
        result.update(decision='not_interpretable', reason='no_contrast')
        return _save(db, plan_row, result)
    result['permutation'] = permutation
    alpha = body['alpha']
    significant = permutation['tail_probability'] <= alpha
    result['trial'] = {
        'decision': 'wording_effect' if significant else 'insufficient_evidence',
        'reason': 'permutation_tail_within_alpha' if significant
        else 'permutation_tail_above_alpha',
    }
    # A causal conclusion is only signed when the registered allocation gives a
    # significant result *and* every delivered answer stayed with its assigned
    # arm and designated wording version.
    result['causal_claim'] = significant and not contaminations and permutation[
        'space_size'] > 1
    result['decision'] = 'verified'
    if contaminations:
        result['reason'] = 'contaminated_delivery'
    elif not significant:
        result['reason'] = 'permutation_tail_above_alpha'
    elif permutation['space_size'] <= 1:
        result['reason'] = 'no_reallocation_space'
    else:
        result['reason'] = 'clean_significant_contrast'
    return _save(db, plan_row, result)


def history(db, course_id, year, form_id, plan_id=None, plan_version=None):
    """Read stored verifications without recomputing them."""
    sql = 'SELECT * FROM trial_verification WHERE course_id=? AND year=? AND form_id=?'
    args = [course_id, year, form_id]
    if plan_id is not None:
        sql += ' AND plan_id=?'
        args.append(plan_id)
    if plan_version is not None:
        sql += ' AND plan_version=?'
        args.append(plan_version)
    sql += ' ORDER BY verification_id'
    return [{'verification_id': item['verification_id'],
             'plan_id': item['plan_id'],
             'plan_version': item['plan_version'],
             'decision': item['decision'],
             'result': json.loads(item['result_json']),
             'sources': json.loads(item['sources_json'])}
            for item in rows(db, sql, args)]
