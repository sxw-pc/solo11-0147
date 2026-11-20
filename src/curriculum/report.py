"""Recompute course qualification across catalog, graph and credit ledgers.

Student course eligibility is decided only by the catalog, prerequisite graph
and credit ledgers.  Handout navigation delivery is reported alongside it from
the independent handout tables and can never change the qualification result.
"""
import json
from . import catalog,prereq,credit,navigation
from .store import rows


def handout_delivery(db, course_id=None, year=None):
    """Current navigation decisions for every signed handout revision."""
    out = []
    for cur in rows(db, 'SELECT * FROM handout_current ORDER BY doc_id'):
        model_doc = cur['doc_id']
        head = db.execute('SELECT course_id,year FROM handout_node WHERE doc_id=? '
                          'AND source_event=? LIMIT 1',
                          (model_doc, cur['source_event'])).fetchone()
        if head is not None and ((course_id and head['course_id'] != course_id)
                                 or (year and head['year'] != year)):
            continue
        decision = navigation.check(db, model_doc, source_event=cur['source_event'])
        out.append({'doc_id': model_doc, 'status': decision['status'],
                    'source_event': cur['source_event'], 'revision': cur['revision'],
                    'contracts': {name: {'decision': c['decision'], 'reason': c['reason']}
                                  for name, c in decision['contracts'].items()}})
    return out


def decide(db,student_id,course_id,year,save=True):
    with db:
        catalog_state=catalog.current(db,course_id,year)
        graph_state=prereq.publish(db,year)
        awards=[]
        for agreement in rows(db,'SELECT DISTINCT target_course FROM equivalence_agreement'):
            awards.append(credit.award(db,student_id,agreement['target_course'],year))
        completed=credit.completed(db,student_id,year)
        qualification=prereq.qualify(db,student_id,course_id,year,completed)
        if catalog_state['decision']!='published':qualification={'student_id':student_id,'course_id':course_id,'year':year,'decision':'pending','reason':'catalog_not_published'}
        # Independent ledger: handout delivery never feeds the qualification.
        handouts=handout_delivery(db,course_id,year)
        result={'student_id':student_id,'course_id':course_id,'year':year,'catalog':catalog_state,'graph':graph_state,'awards':awards,'completed':sorted(completed),'qualification':qualification,'handout_delivery':handouts}
        if save:db.execute('INSERT INTO result_report(student_id,year,body) VALUES (?,?,?)',(student_id,year,json.dumps(result,sort_keys=True)))
        return result
