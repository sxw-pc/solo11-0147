"""Independent graph publication and per-student prerequisite evaluation."""
import hashlib,json
from .sources import record
from .store import rows
from . import catalog


def ingest(db,events):
    count=0
    changed=set()
    with db:
        for e in events:
            if e.get('op')!='rule':raise ValueError('prereq input must be rule')
            if record(db,e,'prereq_rule',e['target_course']):
                db.execute('INSERT INTO prereq_rule VALUES (?,?,?,?)',(e['event_id'],e['target_course'],e['year'],json.dumps(e['rule'],sort_keys=True)))
                count+=1
                changed.add(e['target_course'])
        for year in sorted({r['year'] for r in rows(db,'SELECT DISTINCT year FROM prereq_rule')}):publish(db,year)
        # A changed any/all rule reopens the graduation term of every student
        # whose program depends on the touched course, not just qualification_case.
        from . import plan
        programs=plan.programs_for_courses(db,plan.dependents(db,changed)|changed) if changed else set()
        plans=plan.reverify(db,programs=programs,cause='prereq') if programs else []
    return {'imported':count,'graphs':rows(db,'SELECT * FROM graph_revision'),'plans':[plan.summary(p) for p in plans]}


def latest_rules(db,year):
    records=rows(db,'SELECT p.*,e.event_time FROM prereq_rule p JOIN evidence e USING(event_id) WHERE p.year=? ORDER BY e.event_time DESC,p.event_id DESC',(year,))
    out={}
    for r in records:out.setdefault(r['target_course'],json.loads(r['rule_json']))
    return out


def cycles(rules):
    edges={target:set(rule.get('all',[])+rule.get('any',[])) for target,rule in rules.items()}
    visiting=set();done=set();found=[]
    def walk(node,trail):
        if node in visiting:found.append(trail[trail.index(node):]+[node]);return
        if node in done:return
        visiting.add(node)
        for nxt in sorted(edges.get(node,[])):walk(nxt,trail+[nxt])
        visiting.remove(node);done.add(node)
    for node in sorted(edges):walk(node,[node])
    return found


def publish(db,year):
    rules=latest_rules(db,year);bad=cycles(rules)
    missing=sorted({x for target,r in rules.items() for x in [target]+r.get('all',[])+r.get('any',[]) if catalog.current(db,x,year)['decision']!='published'})
    decision='frozen' if bad or missing else 'published'
    digest=hashlib.sha256(json.dumps(rules,sort_keys=True).encode()).hexdigest()
    db.execute('INSERT INTO graph_revision VALUES (?,?,?,?) ON CONFLICT(year) DO UPDATE SET decision=excluded.decision,cycle_json=excluded.cycle_json,source_hash=excluded.source_hash',
               (year,decision,json.dumps({'cycles':bad,'missing_catalog':missing}),digest))
    return {'year':year,'decision':decision,'cycles':bad,'missing_catalog':missing}


def qualify(db,student_id,course_id,year,completed):
    graph=publish(db,year)
    if graph['decision']!='published':decision='pending';reason='graph_not_published'
    else:
        rule=latest_rules(db,year).get(course_id,{'all':[],'any':[]})
        ok_all=all(c in completed for c in rule.get('all',[]))
        ok_any=not rule.get('any') or any(c in completed for c in rule['any'])
        decision='eligible' if ok_all and ok_any else 'ineligible';reason='requirements_met' if decision=='eligible' else 'missing_prerequisite'
    db.execute('INSERT INTO qualification_case VALUES (?,?,?,?,?) ON CONFLICT(student_id,course_id,year) DO UPDATE SET decision=excluded.decision,reason=excluded.reason',
               (student_id,course_id,year,decision,reason))
    return {'student_id':student_id,'course_id':course_id,'year':year,'decision':decision,'reason':reason}
