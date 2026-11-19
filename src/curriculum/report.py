"""Recompute course qualification across catalog, graph and credit ledgers."""
import json
from . import catalog,prereq,credit
from .store import rows


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
        result={'student_id':student_id,'course_id':course_id,'year':year,'catalog':catalog_state,'graph':graph_state,'awards':awards,'completed':sorted(completed),'qualification':qualification}
        if save:db.execute('INSERT INTO result_report(student_id,year,body) VALUES (?,?,?)',(student_id,year,json.dumps(result,sort_keys=True)))
        return result
