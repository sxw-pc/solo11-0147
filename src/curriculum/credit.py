"""Evidence-backed equivalence, category and anti-double-credit decisions."""
from .sources import record
from .store import rows
from . import catalog


def ingest(db,events):
    count=0
    students=set()
    with db:
        for e in events:
            op=e['op']
            if op=='transcript':
                if record(db,e,'transcript',e['student_id']):
                    db.execute('INSERT INTO transcript_evidence VALUES (?,?,?,?,?,?,?,?)',
                               (e['event_id'],e['student_id'],e['source_course'],e['year'],e['credits'],e['grade'],e['issuer'],e['status']))
                    count+=1
                    students.add(e['student_id'])
            elif op=='agreement':
                if e['category'] not in ('required','elective'):raise ValueError('unknown credit category')
                if record(db,e,'agreement',e['target_course']):
                    db.execute('INSERT INTO equivalence_agreement VALUES (?,?,?,?,?,?,?)',
                               (e['event_id'],e['source_course'],e['target_course'],e['valid_from'],e['valid_until'],e['min_credits'],e['category']))
                    count+=1
                    students|={r['student_id'] for r in rows(db,'SELECT DISTINCT student_id FROM transcript_evidence WHERE source_course=?',(e['source_course'],))}
            else:raise ValueError('credit input must be transcript or agreement')
        # Recompute affected awards after a revocation or a new agreement.
        for r in rows(db,'SELECT DISTINCT student_id,year FROM transcript_evidence'):
            for target in rows(db,'SELECT DISTINCT target_course FROM equivalence_agreement'):
                award(db,r['student_id'],target['target_course'],r['year'])
        # A revoked or newly recognized equivalence reopens the graduation term
        # of the affected students, not just the credit_award rows.
        from . import plan
        plans=plan.reverify(db,students=students,cause='credit') if students else []
    return {'imported':count,'awards':rows(db,'SELECT * FROM credit_award'),'plans':[plan.summary(p) for p in plans]}


def latest_transcripts(db,student_id):
    records=rows(db,'SELECT t.*,e.event_time FROM transcript_evidence t JOIN evidence e USING(event_id) WHERE t.student_id=? ORDER BY e.event_time DESC,t.event_id DESC',(student_id,))
    out={}
    for r in records:out.setdefault(r['source_course'],r)
    return out


def award(db,student_id,target_course,year):
    target=catalog.current(db,target_course,year)
    agreements=rows(db,'SELECT * FROM equivalence_agreement WHERE target_course=? AND valid_from<=? AND valid_until>=? ORDER BY valid_from DESC,event_id DESC',(target_course,year,year))
    transcripts=latest_transcripts(db,student_id)
    chosen=None;reason='no_matching_agreement';category='none'
    if target['decision']!='published':reason='target_not_published'
    else:
        for a in agreements:
            t=transcripts.get(a['source_course'])
            if not t:continue
            chosen=t;category=a['category']
            if t['status']!='passed' or t['grade']<60:reason='transcript_revoked_or_failed';break
            if t['credits']<a['min_credits'] or t['credits']<target['credits']:reason='insufficient_credits';break
            issuer=db.execute('SELECT 1 FROM catalog_source WHERE department=? LIMIT 1',(t['issuer'],)).fetchone()
            if not issuer:reason='untrusted_transcript_issuer';break
            used=db.execute("SELECT target_course FROM credit_award WHERE student_id=? AND source_event=? AND decision='awarded' AND target_course<>?",(student_id,t['event_id'],target_course)).fetchone()
            if used:reason='source_already_spent';break
            reason='agreement_satisfied';break
    decision='awarded' if reason=='agreement_satisfied' else 'rejected'
    # Even a rejection keeps its evidence key; no candidate means choose a stable
    # existing source event for the student, or leave the prior decision untouched.
    source_event=chosen['event_id'] if chosen else next((t['event_id'] for t in transcripts.values()),None)
    if source_event:
        db.execute('INSERT INTO credit_award VALUES (?,?,?,?,?,?,?) ON CONFLICT(student_id,target_course,year) DO UPDATE SET source_event=excluded.source_event,decision=excluded.decision,reason=excluded.reason,category=excluded.category',
                   (student_id,target_course,year,source_event,decision,reason,category))
    return {'student_id':student_id,'target_course':target_course,'year':year,'decision':decision,'reason':reason,'category':category}


def completed(db,student_id,year):
    direct={t['source_course'] for t in latest_transcripts(db,student_id).values() if t['status']=='passed' and t['grade']>=60}
    equivalent={r['target_course'] for r in rows(db,"SELECT target_course FROM credit_award WHERE student_id=? AND year=? AND decision='awarded'",(student_id,year))}
    return direct|equivalent
