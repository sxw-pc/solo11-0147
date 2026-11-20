"""Cross-term completion planning with conditional plans and registrar promises.

The planner schedules remaining program requirements against signed term
offerings, published catalog credits, the per-term credit cap, cross-term
prerequisites and recognized equivalencies.  Choice requirement groups
("choose N of these") are a program-level construct distinct from a course's
own any/all prerequisites, and one course may not fill two mutually exclusive
groups; a recognized source credit is likewise spent once.  Co-enrollment in
the same term never counts as a completed prerequisite.  Offering evidence is
versioned: a same-version available/stopped conflict is a dispute.  A
conditional plan assumes every disputed offering is indeed held; a promise is
issued only for a term that stays feasible under every still-possible
interpretation.  Both conclusions, with the catalog, graph, award and offer
evidence behind them, are appended to plan_case so older verdicts remain
traceable and retransmission stays idempotent.
"""
import json
from itertools import combinations, product

from .store import rows
from . import catalog, prereq, credit

CATEGORIES = ('offering', 'credit_cap', 'prereq', 'equivalence', 'catalog')
_EMPTY_RULE = {'all': [], 'any': []}


# ---------------------------------------------------------------- evidence

def _offer_states(db):
    """Latest signed version per (course, term); same-version disagreement is a dispute."""
    by_pair = {}
    for o in rows(db, 'SELECT * FROM degree_offer'):
        by_pair.setdefault((o['course_id'], o['term']), []).append(o)
    states = {}
    for key, items in by_pair.items():
        top_version = max(i['version'] for i in items)
        top = [i for i in items if i['version'] == top_version]
        values = {i['available'] for i in top}
        state = 'disputed' if len(values) > 1 else ('available' if values.pop() else 'stopped')
        states[key] = {'state': state, 'version': top_version,
                       'events': sorted(i['event_id'] for i in top)}
    return states


def _requirement_spec(db, program_id):
    required = sorted(r['course_id'] for r in rows(
        db, 'SELECT course_id FROM degree_requirement WHERE program_id=?', (program_id,)))
    groups = [{'group_id': g['group_id'], 'choose': g['choose'],
               'options': json.loads(g['options_json'])}
              for g in rows(db, 'SELECT * FROM degree_requirement_group WHERE program_id=? '
                            'ORDER BY group_id', (program_id,))]
    return required, groups


def _equivalence_relaxed_completed(db, student_id, year):
    """Completed set if every satisfiable agreement counted, ignoring the
    one-source-one-target spending rule (used only to relax 'equivalence')."""
    done = set(credit.completed(db, student_id, year))
    transcripts = credit.latest_transcripts(db, student_id)
    for a in rows(db, 'SELECT * FROM equivalence_agreement WHERE valid_from<=? AND valid_until>=?',
                  (year, year)):
        t = transcripts.get(a['source_course'])
        if not t or t['status'] != 'passed' or t['grade'] < 60:
            continue
        target = catalog.current(db, a['target_course'], year)
        if target['decision'] != 'published':
            continue
        if t['credits'] < a['min_credits'] or t['credits'] < target['credits']:
            continue
        if not db.execute('SELECT 1 FROM catalog_source WHERE department=? LIMIT 1',
                          (t['issuer'],)).fetchone():
            continue
        done.add(a['target_course'])
    return done


# ---------------------------------------------------------------- scheduler

class _Ctx:
    """One scheduling problem: terms available, relaxations, interpretation."""

    def __init__(self, base, interpretation, terms, relax):
        self.base = base
        self.terms = list(terms)
        self.relax = frozenset(relax)
        self.needed = base['needed']
        self.optional = base['optional']
        self.groups = base['groups']
        self.cap = base['cap']
        self.rules = base['rules']
        self.completed = base['completed_relaxed'] if 'equivalence' in self.relax else base['completed']
        self.credits = base['credits_relaxed'] if 'catalog' in self.relax else base['credits']
        states = base['offer_states']
        self.availability = {}
        for key, st in states.items():
            if st['state'] == 'available':
                self.availability[key] = True
            elif st['state'] == 'disputed':
                self.availability[key] = bool(interpretation.get(key))
            else:
                self.availability[key] = False

    def relaxed(self, extra):
        return _Ctx(self.base, self.base['interpretation'], self.terms, self.relax | set(extra))

    def offer_ok(self, course, term):
        if 'offering' in self.relax:
            return True
        return self.availability.get((course, term), False)


def _groups_ok(groups, completed, scheduled, allow_reuse):
    """Each group needs `choose` distinct satisfied options; without
    relaxation one course may not fill two mutually exclusive groups."""
    satisfied = {g['group_id']: {c for c in g['options'] if c in completed or c in scheduled}
                 for g in groups}
    if allow_reuse:
        return all(len(satisfied[g['group_id']]) >= g['choose'] for g in groups)
    order = sorted(groups, key=lambda g: (len(satisfied[g['group_id']]), g['group_id']))
    used = set()

    def go(i):
        if i == len(order):
            return True
        g = order[i]
        avail = sorted(satisfied[g['group_id']] - used)
        if len(avail) < g['choose']:
            return False
        for combo in combinations(avail, g['choose']):
            used.update(combo)
            if go(i + 1):
                return True
            used.difference_update(combo)
        return False

    return go(0)


def _partial_prereq_ok(ctx, assign, course, term):
    rule = ctx.rules.get((course, term), _EMPTY_RULE)
    for p in rule.get('all', []):
        if p in ctx.completed:
            continue
        pt = assign.get(p)
        if pt is not None and pt >= term:
            return False
    for other, ot in assign.items():
        if ot is None:
            continue
        other_rule = ctx.rules.get((other, ot), _EMPTY_RULE)
        if course in other_rule.get('all', []) and course not in ctx.completed and term >= ot:
            return False
    return True


def _search(ctx):
    """Assign every needed course (and the optional ones that prove necessary)
    to a term.  Deterministic: needed courses first, earliest term first."""
    order = list(ctx.needed) + list(ctx.optional)
    assign = {}
    loads = {t: 0 for t in ctx.terms}

    def leaf_ok():
        scheduled = {c: t for c, t in assign.items() if t is not None}
        if 'prereq' not in ctx.relax:
            for c, t in scheduled.items():
                rule = ctx.rules.get((c, t), _EMPTY_RULE)
                for p in rule.get('all', []):
                    if p in ctx.completed:
                        continue
                    if p not in scheduled or scheduled[p] >= t:
                        return False
                anys = rule.get('any', [])
                if anys and not any(p in ctx.completed or
                                    (p in scheduled and scheduled[p] < t) for p in anys):
                    return False
        return _groups_ok(ctx.groups, ctx.completed, scheduled, 'equivalence' in ctx.relax)

    def dfs(i):
        if i == len(order):
            return dict(assign) if leaf_ok() else None
        c = order[i]
        values = ([None] if c in ctx.optional else []) + list(ctx.terms)
        for v in values:
            if v is None:
                assign[c] = None
                found = dfs(i + 1)
                if found is not None:
                    return found
                del assign[c]
                continue
            if not ctx.offer_ok(c, v):
                continue
            cr = ctx.credits.get((c, v))
            if cr is None:
                continue
            if 'credit_cap' not in ctx.relax and loads[v] + cr > ctx.cap:
                continue
            if 'prereq' not in ctx.relax and not _partial_prereq_ok(ctx, assign, c, v):
                continue
            assign[c] = v
            loads[v] += cr
            found = dfs(i + 1)
            if found is not None:
                return found
            loads[v] -= cr
            del assign[c]
        return None

    return dfs(0)


def _key_constraints(ctx):
    """Minimal relaxation sets that would make this term feasible; their union
    is the key constraint set an advisor reviews (offering, credit cap,
    prerequisite, equivalence or catalog)."""
    minimal = []
    for size in range(1, len(CATEGORIES) + 1):
        for combo in combinations(CATEGORIES, size):
            if any(set(m) <= set(combo) for m in minimal):
                continue
            if _search(ctx.relaxed(combo)) is not None:
                minimal.append(combo)
    if not minimal:
        keys = []
        if any(all(ctx.credits.get((c, t)) is None for t in ctx.terms) for c in ctx.needed):
            keys.append('catalog')
        if not ctx.terms:
            keys.append('offering')
        return (sorted(set(keys)) or ['offering']), []
    return sorted({c for m in minimal for c in m}), [sorted(m) for m in minimal]


def _format_schedule(ctx, solution):
    by_term = {}
    for c, t in solution.items():
        if t is not None:
            by_term.setdefault(t, []).append(c)
    out = []
    for t in sorted(by_term):
        courses = [{'course_id': c, 'credits': ctx.credits[(c, t)]} for c in sorted(by_term[t])]
        out.append({'term': t, 'courses': courses,
                    'credits': sum(c['credits'] for c in courses)})
    return out


def _run(base, interpretation):
    """Earliest feasible term under one dispute interpretation, plus the key
    constraints behind every earlier candidate term."""
    base = dict(base, interpretation=interpretation)
    if not base['candidates']:
        ctx = _Ctx(base, interpretation, [], frozenset())
        keys, minima = _key_constraints(ctx)
        return {'earliest': None, 'schedule': None,
                'per_term': [{'term': None, 'key_constraints': keys,
                              'minimal_relaxations': minima}]}
    per_term = []
    for term in base['candidates']:
        ctx = _Ctx(base, interpretation, [t for t in base['candidates'] if t <= term], frozenset())
        solution = _search(ctx)
        if solution is not None:
            return {'earliest': term, 'schedule': _format_schedule(ctx, solution),
                    'per_term': per_term}
        keys, minima = _key_constraints(ctx)
        per_term.append({'term': term, 'key_constraints': keys, 'minimal_relaxations': minima})
    return {'earliest': None, 'schedule': None, 'per_term': per_term}


def _promise_infeasible_before(branches, candidates, upto):
    out = []
    for term in [t for t in candidates if upto is None or t < upto]:
        entries = []
        for b in branches:
            earliest = b['run']['earliest']
            if earliest is not None and earliest <= term:
                continue
            pt = next((p for p in b['run']['per_term'] if p['term'] == term), None)
            if pt:
                entries.append({'interpretation': b['interpretation'],
                                'key_constraints': pt['key_constraints']})
        if entries:
            out.append({'term': term, 'branches': entries})
    return out


# ---------------------------------------------------------------- verdicts

def verify(db, student_id, program_id, cause='manual', save=True):
    """Re-verify the earliest completion term for one student and program.

    Returns the conditional plan (disputed offerings assumed held) and the
    unconditional promise (feasible under every still-possible offering
    interpretation), each with an executable per-term schedule and, for every
    earlier candidate term, the key constraints that make it impossible."""
    profile = db.execute(
        'SELECT p.*, e.event_time FROM degree_profile p JOIN evidence e USING(event_id) '
        'WHERE p.student_id=? AND p.program_id=? '
        'ORDER BY e.event_time DESC, p.event_id DESC LIMIT 1',
        (student_id, program_id)).fetchone()
    if not profile:
        return {'student_id': student_id, 'program_id': program_id, 'state': 'no_profile'}
    start_term = profile['start_term']
    cap = profile['max_credits']
    as_of_year = start_term // 10

    required, groups = _requirement_spec(db, program_id)
    completed = credit.completed(db, student_id, as_of_year)
    completed_relaxed = _equivalence_relaxed_completed(db, student_id, as_of_year)
    offer_states = _offer_states(db)

    needed = sorted(set(required) - completed)
    universe = set(needed) | {c for g in groups for c in g['options'] if c not in completed}
    rule_years = {as_of_year} | {r['year'] for r in rows(db, 'SELECT DISTINCT year FROM prereq_rule')}
    closure_rules = {y: prereq.latest_rules(db, y) for y in rule_years}
    stack = list(universe)
    while stack:
        course = stack.pop()
        for rules in closure_rules.values():
            rule = rules.get(course, _EMPTY_RULE)
            for p in rule.get('all', []) + rule.get('any', []):
                if p not in completed and p not in universe:
                    universe.add(p)
                    stack.append(p)

    candidates = sorted({t for (c, t), st in offer_states.items()
                         if c in universe and t >= start_term and st['state'] != 'stopped'})
    disputes = sorted([(c, t) for (c, t), st in offer_states.items()
                       if c in universe and t >= start_term and st['state'] == 'disputed'])
    years = {as_of_year} | {t // 10 for t in candidates}
    rules_by_year = {y: closure_rules.get(y, prereq.latest_rules(db, y)) for y in years}
    rules = {(c, t): rules_by_year[t // 10].get(c, _EMPTY_RULE)
             for c in universe for t in candidates}
    credits, credits_relaxed = {}, {}
    for c in universe:
        for t in candidates:
            cur = catalog.current(db, c, t // 10)
            cr = cur.get('credits') if cur['decision'] == 'published' else None
            credits[(c, t)] = cr
            if cr is None:
                r = db.execute('SELECT credits FROM course_revision WHERE course_id=? AND year=? '
                               'ORDER BY version DESC, event_id DESC LIMIT 1',
                               (c, t // 10)).fetchone()
                cr = r['credits'] if r else None
            credits_relaxed[(c, t)] = cr

    base = {'needed': needed, 'optional': sorted(universe - set(needed)), 'groups': groups,
            'cap': cap, 'rules': rules, 'credits': credits, 'credits_relaxed': credits_relaxed,
            'completed': completed, 'completed_relaxed': completed_relaxed,
            'offer_states': offer_states, 'candidates': candidates}
    evidence = _evidence(db, student_id, as_of_year, years, universe, offer_states,
                         disputes, completed)
    requirements = {'required': required, 'groups': groups}

    if not needed and _groups_ok(groups, completed, {}, False):
        conditional = {'state': 'complete', 'term': start_term, 'schedule': [],
                       'assumptions': [], 'infeasible_before': []}
        promise = {'state': 'complete', 'term': start_term, 'branches': [],
                   'infeasible_before': []}
    else:
        cond_run = _run(base, {d: True for d in disputes})
        conditional = {
            'state': 'planned' if cond_run['earliest'] is not None else 'infeasible',
            'term': cond_run['earliest'],
            'schedule': cond_run['schedule'] or [],
            'assumptions': [{'course_id': c, 'term': t, 'assumed': 'available',
                             'version': offer_states[(c, t)]['version'],
                             'events': offer_states[(c, t)]['events']} for c, t in disputes],
            'infeasible_before': cond_run['per_term']}
        if conditional['state'] == 'infeasible':
            conditional['key_constraints'] = (
                cond_run['per_term'][-1]['key_constraints'] if cond_run['per_term'] else [])

        branches = []
        for values in product([False, True], repeat=len(disputes)):
            interpretation = dict(zip(disputes, values))
            run = _run(base, interpretation)
            branches.append({'interpretation': [
                {'course_id': c, 'term': t, 'available': interpretation[(c, t)]}
                for c, t in disputes], 'run': run})
        branches_out = [{'interpretation': b['interpretation'],
                         'term': b['run']['earliest'],
                         'schedule': b['run']['schedule'] or []} for b in branches]
        if all(b['run']['earliest'] is not None for b in branches):
            term = max(b['run']['earliest'] for b in branches)
            promise = {'state': 'planned', 'term': term, 'branches': branches_out,
                       'infeasible_before': _promise_infeasible_before(branches, candidates, term)}
        else:
            keys = sorted({k for b in branches for p in b['run']['per_term']
                           for k in p['key_constraints']})
            promise = {'state': 'infeasible', 'term': None, 'branches': branches_out,
                       'infeasible_before': _promise_infeasible_before(branches, candidates, None),
                       'key_constraints': keys}

    result = {'student_id': student_id, 'program_id': program_id, 'as_of_year': as_of_year,
              'start_term': start_term, 'requirements': requirements,
              'conditional': conditional, 'promise': promise, 'evidence': evidence}
    if save:
        _persist(db, result, cause)
    return result


def _persist(db, result, cause):
    """Append changed conclusions; identical re-verification stays idempotent."""
    for kind in ('conditional', 'promise'):
        section = result[kind]
        body = json.dumps({'section': section, 'evidence': result['evidence']},
                          ensure_ascii=False, sort_keys=True)
        old = db.execute('SELECT body FROM plan_case WHERE student_id=? AND program_id=? '
                         'AND kind=? ORDER BY id DESC LIMIT 1',
                         (result['student_id'], result['program_id'], kind)).fetchone()
        if old and old['body'] == body:
            continue
        db.execute('INSERT INTO plan_case(student_id,program_id,kind,state,term,cause,body) '
                   'VALUES (?,?,?,?,?,?,?)',
                   (result['student_id'], result['program_id'], kind, section['state'],
                    section['term'], cause, body))


def _evidence(db, student_id, as_of_year, years, universe, offer_states, disputes, completed):
    catalog_ev = {}
    for c in sorted(universe):
        catalog_ev[c] = {str(y): {'decision': (cur := catalog.current(db, c, y))['decision'],
                                  'credits': cur.get('credits'),
                                  'event_id': cur.get('event_id')} for y in sorted(years)}
    graphs = {str(r['year']): {'decision': r['decision'], 'source_hash': r['source_hash']}
              for r in rows(db, 'SELECT * FROM graph_revision') if r['year'] in years}
    awards = rows(db, 'SELECT target_course, decision, reason, category, source_event '
                      'FROM credit_award WHERE student_id=? AND year=? ORDER BY target_course',
                  (student_id, as_of_year))
    if universe:
        marks = ','.join('?' for _ in universe)
        offers = rows(db, 'SELECT event_id, course_id, term, available, version FROM degree_offer '
                          f'WHERE course_id IN ({marks}) ORDER BY course_id, term, version, event_id',
                      tuple(sorted(universe)))
    else:
        offers = []
    return {'completed': sorted(completed), 'catalog': catalog_ev, 'graph': graphs,
            'awards': awards, 'offers': offers,
            'disputes': [{'course_id': c, 'term': t, 'version': offer_states[(c, t)]['version'],
                          'events': offer_states[(c, t)]['events']} for c, t in disputes]}


# ---------------------------------------------------------------- triggers

def dependents(db, courses):
    """Courses that transitively require any of the given ones (any year)."""
    reverse = {}
    for r in rows(db, 'SELECT target_course, rule_json FROM prereq_rule'):
        rule = json.loads(r['rule_json'])
        for p in rule.get('all', []) + rule.get('any', []):
            reverse.setdefault(p, set()).add(r['target_course'])
    affected = set(courses)
    stack = list(affected)
    while stack:
        course = stack.pop()
        for target in reverse.get(course, ()):
            if target not in affected:
                affected.add(target)
                stack.append(target)
    return affected


def programs_for_courses(db, courses):
    courses = set(courses)
    if not courses:
        return set()
    marks = ','.join('?' for _ in courses)
    programs = {r['program_id'] for r in rows(
        db, f'SELECT DISTINCT program_id FROM degree_requirement WHERE course_id IN ({marks})',
        tuple(sorted(courses)))}
    for g in rows(db, 'SELECT program_id, options_json FROM degree_requirement_group'):
        if set(json.loads(g['options_json'])) & courses:
            programs.add(g['program_id'])
    return programs


def reverify(db, students=None, programs=None, cause='manual'):
    """Re-verify plans for the affected profiles after new evidence arrived."""
    pairs = rows(db, 'SELECT DISTINCT student_id, program_id FROM degree_profile')
    out = []
    for p in sorted(pairs, key=lambda r: (r['student_id'], r['program_id'])):
        if students is not None and p['student_id'] not in students:
            continue
        if programs is not None and p['program_id'] not in programs:
            continue
        out.append(verify(db, p['student_id'], p['program_id'], cause=cause))
    return out


def summary(verdict):
    if 'conditional' not in verdict:
        return {'student_id': verdict['student_id'], 'program_id': verdict['program_id'],
                'state': verdict['state']}
    return {'student_id': verdict['student_id'], 'program_id': verdict['program_id'],
            'conditional_term': verdict['conditional']['term'],
            'promise_term': verdict['promise']['term'],
            'state': verdict['promise']['state']}


def latest(db, student_id, program_id):
    out = {}
    for kind in ('conditional', 'promise'):
        r = db.execute('SELECT * FROM plan_case WHERE student_id=? AND program_id=? AND kind=? '
                       'ORDER BY id DESC LIMIT 1', (student_id, program_id, kind)).fetchone()
        if r:
            d = dict(r)
            d['body'] = json.loads(r['body'])
            out[kind] = d
    return out


def history(db, student_id, program_id, kind=None):
    sql = 'SELECT * FROM plan_case WHERE student_id=? AND program_id=?'
    args = [student_id, program_id]
    if kind:
        sql += ' AND kind=?'
        args.append(kind)
    return [dict(r) for r in db.execute(sql + ' ORDER BY id', args).fetchall()]
