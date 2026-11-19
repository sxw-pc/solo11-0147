"""Independent academic program requirements and offering intake."""
from .sources import record
from .store import rows
from . import credit


def ingest(db, data):
    affected = set()
    count = 0
    with db:
        for item in data.get('requirements', []):
            if record(db, item, 'degree-requirement', item['program_id']):
                db.execute('INSERT INTO degree_requirement VALUES (?,?,?)',
                           (item['event_id'], item['program_id'], item['course_id']))
                count += 1
                affected.add(item['program_id'])
        for item in data.get('offers', []):
            if record(db, item, 'degree-offer', item['course_id']):
                db.execute('INSERT INTO degree_offer VALUES (?,?,?,?)',
                           (item['event_id'], item['course_id'], item['term'],
                            int(item['available'])))
                count += 1
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
    return {'imported': count, 'cases': cases}


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
