"""Independent invitation collection and basic response-rate ledger."""
from .sources import record
from .store import rows
from . import survey


def ingest(db, data):
    affected = set()
    count = 0
    with db:
        for item in data.get('invitations', []):
            if not item['response_token'] or not item['cohort']:
                raise ValueError('invitation needs token and cohort')
            if record(db, item, 'survey-invitation', item['response_token']):
                db.execute('INSERT INTO survey_invitation VALUES (?,?,?,?,?,?,?)',
                           (item['event_id'], item['course_id'], item['year'],
                            item['form_id'], item['response_token'], item['cohort'],
                            int(item['eligible'])))
                count += 1
                affected.add((item['course_id'], item['year'], item['form_id']))
        cases = [decide(db, *key) for key in sorted(affected)]
    return {'imported': count, 'cases': cases}


def decide(db, course_id, year, form_id):
    invitations = rows(
        db, 'SELECT i.*,e.event_time FROM survey_invitation i JOIN evidence e USING(event_id) '
            'WHERE i.course_id=? AND i.year=? AND i.form_id=? '
            'ORDER BY e.event_time DESC,i.event_id DESC',
        (course_id, year, form_id),
    )
    latest = {}
    for item in invitations:
        latest.setdefault(item['response_token'], item)
    eligible = {token for token, item in latest.items() if item['eligible']}
    responses = rows(db, 'SELECT DISTINCT response_token FROM survey_response '
                    'WHERE course_id=? AND year=? AND form_id=?',
                    (course_id, year, form_id))
    responded = len(eligible & {item['response_token'] for item in responses})
    invited = len(eligible)
    rate = responded / invited if invited else None
    decision = 'reported' if survey.current_form(db, course_id, year, form_id)['decision'] == 'published' and invited else 'pending'
    db.execute('INSERT INTO survey_roster_case VALUES (?,?,?,?,?,?,?) '
               'ON CONFLICT(course_id,year,form_id) DO UPDATE SET '
               'invited=excluded.invited,responded=excluded.responded,rate=excluded.rate,decision=excluded.decision',
               (course_id, year, form_id, invited, responded, rate, decision))
    return {'course_id': course_id, 'year': year, 'form_id': form_id,
            'invited': invited, 'responded': responded, 'rate': rate,
            'decision': decision}
