"""Independent academic program requirements and offering intake."""
import json
from .sources import record
from .store import rows
from . import credit


def ingest(db, data):
    affected = set()
    offer_courses = set()
    count = 0
    with db:
        for item in data.get('requirements', []):
            if record(db, item, 'degree-requirement', item['program_id']):
                db.execute('INSERT INTO degree_requirement VALUES (?,?,?)',
                           (item['event_id'], item['program_id'], item['course_id']))
                count += 1
                affected.add(item['program_id'])
        for item in data.get('requirement_groups', []):
            options = sorted(item['options'])
            choose = int(item.get('choose', 1))
            if choose < 1 or choose > len(options):
                raise ValueError('invalid requirement group choose')
            if record(db, item, 'degree-requirement-group', item['program_id']):
                db.execute('INSERT INTO degree_requirement_group VALUES (?,?,?,?,?)',
                           (item['event_id'], item['program_id'], item['group_id'], choose,
                            json.dumps(options)))
                count += 1
                affected.add(item['program_id'])
        for item in data.get('offers', []):
            if int(item.get('version', 1)) < 1:
                raise ValueError('invalid offer version')
            if record(db, item, 'degree-offer', item['course_id']):
                db.execute('INSERT INTO degree_offer(event_id,course_id,term,available,version) '
                           'VALUES (?,?,?,?,?)',
                           (item['event_id'], item['course_id'], item['term'],
                            int(item['available']), int(item.get('version', 1))))
                count += 1
                offer_courses.add(item['course_id'])
        for item in data.get('profiles', []):
            if item['max_credits'] < 1:
                raise ValueError('invalid term credit cap')
            if record(db, item, 'degree-profile', item['student_id']):
                db.execute('INSERT INTO degree_profile VALUES (?,?,?,?,?)',
                           (item['event_id'], item['student_id'], item['program_id'],
                            item['start_term'], item['max_credits']))
                count += 1
                affected.add(item['program_id'])
        cases = []
        for program in sorted(affected):
            profiles = rows(db, 'SELECT * FROM degree_profile WHERE program_id=?', (program,))
            cases.extend(decide(db, p['student_id'], program, p['start_term'] // 10)
                         for p in profiles)
        # A changed requirement, profile or signed offering reopens the whole
        # term plan of every program depending on the touched courses.
        from . import plan
        plan_programs = set(affected)
        if offer_courses:
            plan_programs |= plan.programs_for_courses(
                db, plan.dependents(db, offer_courses) | offer_courses)
        plans = plan.reverify(db, programs=plan_programs, cause='degree') if plan_programs else []
    return {'imported': count, 'cases': cases, 'plans': [plan.summary(p) for p in plans]}


def decide(db, student_id, program_id, year):
    required = {r['course_id'] for r in rows(db, 'SELECT course_id FROM degree_requirement '
                                              'WHERE program_id=?', (program_id,))}
    completed = credit.completed(db, student_id, year)
    done = required & completed
    missing = required - done
    state = 'complete' if required and not missing else 'incomplete'
    db.execute('INSERT INTO degree_case VALUES (?,?,?,?,?,?) '
               'ON CONFLICT(student_id,program_id,year) DO UPDATE SET '
               'completed_count=excluded.completed_count,missing_count=excluded.missing_count,'
               'state=excluded.state',
               (student_id, program_id, year, len(done), len(missing), state))
    return {'student_id': student_id, 'program_id': program_id, 'year': year,
            'completed_count': len(done), 'missing_count': len(missing),
            'state': state, 'missing': sorted(missing)}
