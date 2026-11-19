import json,sys,tempfile,unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
import curriculum
from curriculum import catalog,prereq,credit,report
from curriculum.store import connect
print('imported module:',curriculum.__file__)
assert Path(curriculum.__file__).resolve().is_relative_to(ROOT)

def fixture(name):return json.loads((ROOT/'fixtures'/'curriculum'/name).read_text(encoding='utf-8'))

class CurriculumCases(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.db=connect(Path(self.tmp.name)/'test.sqlite')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def good(self):
        catalog.ingest(self.db,fixture('catalog_good.json'))
        prereq.ingest(self.db,fixture('prereq_good.json'))
        credit.ingest(self.db,fixture('credit_good.json'))

    def test_three_ledgers_qualify_student(self):
        self.good();r=report.decide(self.db,'student-1','C3',2026)
        self.assertEqual((r['catalog']['decision'],r['graph']['decision'],r['awards'][0]['decision'],r['qualification']['decision']),('published','published','awarded','eligible'))

    def test_catalog_same_version_conflict_freezes_graph(self):
        self.good();catalog.ingest(self.db,fixture('catalog_conflict.json'))
        r=report.decide(self.db,'student-1','C3',2026)
        self.assertEqual(catalog.current(self.db,'C2',2026)['decision'],'disputed')
        self.assertEqual((r['graph']['decision'],r['qualification']['decision']),('frozen','pending'))

    def test_catalog_credit_revision_reopens_equivalence(self):
        self.good()
        revised=dict(fixture('catalog_good.json')[2],event_id='c2-v2',version=2,credits=4,event_time='2026-02-10T00:00:00Z')
        catalog.ingest(self.db,[revised])
        r=report.decide(self.db,'student-1','C3',2026)
        self.assertEqual((r['catalog']['decision'],r['graph']['decision']),('published','published'))
        self.assertEqual((r['awards'][0]['reason'],r['qualification']['decision']),('insufficient_credits','ineligible'))

    def test_graph_cycle_independently_freezes_qualification(self):
        self.good();prereq.ingest(self.db,fixture('prereq_cycle.json'))
        r=report.decide(self.db,'student-1','C3',2026)
        self.assertEqual(r['catalog']['decision'],'published')
        self.assertEqual((r['graph']['decision'],r['qualification']['decision']),('frozen','pending'))
        self.assertTrue(r['graph']['cycles'])

    def test_equivalence_revocation_changes_prereq_only(self):
        self.good()
        revoked=dict(fixture('credit_good.json')[1],event_id='t-x1-revoke',status='revoked',event_time='2026-03-01T00:00:00Z')
        credit.ingest(self.db,[revoked])
        r=report.decide(self.db,'student-1','C3',2026)
        self.assertEqual((r['catalog']['decision'],r['graph']['decision']),('published','published'))
        self.assertEqual((r['awards'][0]['decision'],r['qualification']['decision']),('rejected','ineligible'))

    def test_duplicate_source_cannot_fund_two_targets(self):
        self.good();credit.ingest(self.db,fixture('credit_duplicate.json'))
        r=report.decide(self.db,'student-1','C3',2026)
        awards={a['target_course']:a for a in r['awards']}
        self.assertEqual((awards['C2']['decision'],awards['C1']['reason']),('awarded','source_already_spent'))

    def test_stopped_course_keeps_old_year(self):
        self.good()
        new=dict(fixture('catalog_good.json')[2],event_id='c2-stop',year=2027,version=2,status='stopped',event_time='2027-01-01T00:00:00Z')
        c1=dict(fixture('catalog_good.json')[1],event_id='c1-2027',year=2027,event_time='2027-01-01T00:00:00Z')
        c3=dict(fixture('catalog_good.json')[3],event_id='c3-2027',year=2027,event_time='2027-01-01T00:00:00Z')
        catalog.ingest(self.db,[new,c1,c3])
        r=dict(fixture('prereq_good.json')[0],event_id='r-c3-2027',year=2027,event_time='2027-01-02T00:00:00Z')
        prereq.ingest(self.db,[r])
        self.assertEqual(catalog.current(self.db,'C2',2026)['decision'],'published')
        self.assertEqual(catalog.current(self.db,'C2',2027)['decision'],'stopped')
        self.assertEqual(prereq.publish(self.db,2027)['decision'],'frozen')
        self.assertEqual(prereq.publish(self.db,2026)['decision'],'published')

    def test_untrusted_catalog_source_is_not_published(self):
        item=dict(fixture('catalog_good.json')[3],event_id='bad-source',source_id='unknown')
        catalog.ingest(self.db,[item])
        self.assertEqual(catalog.current(self.db,'C3',2026)['decision'],'untrusted')

    def test_expired_equivalence_does_not_award_new_year(self):
        self.good();a=credit.award(self.db,'student-1','C2',2027)
        self.assertEqual(a['decision'],'rejected')

    def test_duplicate_event_id_conflict_rolls_back(self):
        self.good();modified=dict(fixture('credit_good.json')[1],grade=0)
        with self.assertRaises(ValueError):credit.ingest(self.db,[dict(modified,event_id='new'),modified])
        self.assertIsNone(self.db.execute("SELECT event_id FROM evidence WHERE event_id='new'").fetchone())

    def test_and_or_prerequisite_requires_each_branch(self):
        self.good();r=prereq.qualify(self.db,'student-1','C3',2026,{'C1'})
        self.assertEqual(r['decision'],'ineligible')
        r=prereq.qualify(self.db,'student-1','C3',2026,{'C1','C2'})
        self.assertEqual(r['decision'],'eligible')

    def test_credit_category_persisted(self):
        self.good();r=self.db.execute("SELECT category,decision FROM credit_award WHERE target_course='C2'").fetchone()
        self.assertEqual((r['category'],r['decision']),('required','awarded'))

def entry(course,credits=3,status='open'):return {'course_id':course,'credits':credits,'status':status}

def source_event(event_id='src-d',time='2026-01-01T00:00:00Z'):
    return {'event_id':event_id,'op':'source','source_id':'dept-d','department':'D','valid_from':2025,'valid_until':2027,'event_time':time}

def batch_decl(event_id,batch_id,kind='complete',version=1,year=2026,page_count=1,declared=1,source='dept-d',dept='D',time='2026-04-01T00:00:00Z'):
    return {'event_id':event_id,'op':'export_batch','batch_id':batch_id,'source_id':source,'department':dept,'year':year,'kind':kind,'version':version,'page_count':page_count,'declared_courses':declared,'event_time':time}

def batch_page(event_id,batch_id,page,courses,time='2026-04-02T00:00:00Z',digest=None):
    return {'event_id':event_id,'op':'export_page','batch_id':batch_id,'page':page,'page_digest':digest or catalog.page_digest(courses),'courses':courses,'event_time':time}

class ExportBatchCases(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.db=connect(Path(self.tmp.name)/'test.sqlite')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def batch(self,batch_id):return dict(self.db.execute('SELECT * FROM export_batch WHERE batch_id=?',(batch_id,)).fetchone())
    def full(self,batch_id,version,courses,**kw):
        kw.setdefault('declared',len(courses))
        return [batch_decl(f'decl-{batch_id}',batch_id,version=version,**kw),batch_page(f'page-{batch_id}-1',batch_id,1,courses)]

    def test_complete_batch_absence_stops_course_but_patch_does_not(self):
        catalog.ingest(self.db,[source_event()]+self.full('full-1',1,[entry('C1'),entry('C2'),entry('C3')]))
        prereq.ingest(self.db,fixture('prereq_good.json'))
        self.assertEqual(catalog.current(self.db,'C2',2026)['decision'],'published')
        catalog.ingest(self.db,self.full('full-2',2,[entry('C1'),entry('C3')]))
        gone=catalog.current(self.db,'C2',2026)
        self.assertEqual((gone['decision'],gone['status'],gone['batch_id'],gone['page']),('stopped','stopped','full-2',None))
        self.assertEqual(gone['origin_event'],'decl-full-2')
        self.assertEqual(prereq.publish(self.db,2026)['decision'],'frozen')
        r=report.decide(self.db,'student-1','C3',2026)
        self.assertEqual((r['catalog']['batch_id'],r['catalog']['page'],r['catalog']['origin_event']),('full-2',1,'page-full-2-1'))
        self.assertEqual((r['graph']['decision'],r['qualification']['decision']),('frozen','pending'))

    def test_patch_leaves_unlisted_course_on_old_evidence(self):
        catalog.ingest(self.db,[source_event()]+self.full('full-1',1,[entry('C1'),entry('C2'),entry('C3')]))
        prereq.ingest(self.db,fixture('prereq_good.json'))
        catalog.ingest(self.db,self.full('patch-1',2,[entry('C1',credits=4),entry('C3')],kind='patch'))
        self.assertEqual(self.batch('patch-1')['state'],'complete')
        kept=catalog.current(self.db,'C2',2026)
        self.assertEqual((kept['decision'],kept['batch_id']),('published','full-1'))
        self.assertEqual(catalog.current(self.db,'C1',2026)['credits'],4)
        self.assertEqual(prereq.publish(self.db,2026)['decision'],'published')

    def test_missing_pages_keep_batch_pending_until_supplied(self):
        catalog.ingest(self.db,[source_event()]+self.full('full-1',1,[entry('C1'),entry('C2'),entry('C3')]))
        catalog.ingest(self.db,[batch_decl('decl-full-2','full-2',version=2,page_count=2,declared=2),batch_page('page-full-2-1','full-2',1,[entry('C1')])])
        b=self.batch('full-2')
        self.assertEqual((b['state'],b['reason'],json.loads(b['detail'])['missing']),('pending','awaiting_pages',[2]))
        self.assertEqual(catalog.current(self.db,'C2',2026)['decision'],'published')
        catalog.ingest(self.db,[batch_page('page-full-2-2','full-2',2,[entry('C3')])])
        self.assertEqual(self.batch('full-2')['state'],'complete')
        self.assertEqual(catalog.current(self.db,'C2',2026)['decision'],'stopped')

    def test_cross_page_duplicate_and_conflict_fail_batch(self):
        catalog.ingest(self.db,[source_event(),
            batch_decl('decl-dup','full-dup',page_count=2,declared=2),
            batch_page('page-dup-1','full-dup',1,[entry('C1')]),batch_page('page-dup-2','full-dup',2,[entry('C1')]),
            batch_decl('decl-clash','full-clash',page_count=2,declared=2),
            batch_page('page-clash-1','full-clash',1,[entry('C2',credits=3)]),batch_page('page-clash-2','full-clash',2,[entry('C2',credits=4)])])
        self.assertEqual(self.batch('full-dup')['reason'],'duplicate_course')
        self.assertEqual(self.batch('full-clash')['reason'],'cross_page_conflict')
        self.assertEqual(catalog.current(self.db,'C1',2026)['decision'],'missing')
        self.assertEqual(catalog.current(self.db,'C2',2026)['decision'],'missing')

    def test_page_digest_and_declared_count_checks(self):
        bad=batch_page('page-bad-1','full-bad',1,[entry('C1')],digest='0'*64)
        catalog.ingest(self.db,[source_event(),batch_decl('decl-bad','full-bad',declared=1),bad,
                                batch_decl('decl-short','full-short',declared=2),batch_page('page-short-1','full-short',1,[entry('C2')])])
        self.assertEqual(self.batch('full-bad')['reason'],'page_digest_mismatch')
        short=self.batch('full-short')
        self.assertEqual((short['state'],short['reason']),('failed','count_mismatch'))
        self.assertEqual(catalog.current(self.db,'C1',2026)['decision'],'missing')

    def test_export_source_authority_enforced(self):
        catalog.ingest(self.db,[source_event(),
            batch_decl('decl-unknown','full-unknown',source='unknown'),batch_page('page-unknown-1','full-unknown',1,[entry('C9')]),
            batch_decl('decl-dept','full-dept',dept='E'),batch_page('page-dept-1','full-dept',1,[entry('C8')]),
            batch_decl('decl-year','full-year',year=2030),batch_page('page-year-1','full-year',1,[entry('C7')])])
        for course,year in (('C9',2026),('C8',2026),('C7',2030)):
            self.assertEqual(catalog.current(self.db,course,year)['decision'],'untrusted')

    def test_same_version_batches_dispute_only_affected_course(self):
        catalog.ingest(self.db,[source_event()]
            +self.full('full-a',1,[entry('C1',credits=3),entry('C2'),entry('C3')],declared=3)
            +self.full('full-b',1,[entry('C1',credits=4),entry('C2'),entry('C3')],declared=3))
        self.assertEqual(catalog.current(self.db,'C1',2026)['decision'],'disputed')
        self.assertEqual(catalog.current(self.db,'C2',2026)['decision'],'published')
        self.assertEqual(catalog.current(self.db,'C3',2026)['decision'],'published')

    def test_patch_and_complete_batch_opposite_status_disputes(self):
        catalog.ingest(self.db,[source_event()]
            +self.full('full-1',2,[entry('C1'),entry('C2')],declared=2)
            +self.full('patch-1',2,[entry('C2',status='stopped')],kind='patch'))
        self.assertEqual(catalog.current(self.db,'C2',2026)['decision'],'disputed')
        self.assertEqual(catalog.current(self.db,'C1',2026)['decision'],'published')

    def test_later_higher_version_batch_recomputes_and_keeps_evidence(self):
        catalog.ingest(self.db,[source_event()]
            +self.full('full-a',1,[entry('C1',credits=3)],declared=1)
            +self.full('full-b',1,[entry('C1',credits=4)],declared=1))
        self.assertEqual(catalog.current(self.db,'C1',2026)['decision'],'disputed')
        catalog.ingest(self.db,self.full('full-c',2,[entry('C1',credits=3)]))
        won=catalog.current(self.db,'C1',2026)
        self.assertEqual((won['decision'],won['credits'],won['batch_id']),('published',3,'full-c'))
        kept={r['event_id'] for r in self.db.execute("SELECT event_id FROM evidence WHERE kind LIKE 'export%'").fetchall()}
        self.assertTrue({'decl-full-a','page-full-a-1','decl-full-b','page-full-b-1'}<=kept)
        self.assertEqual(self.db.execute("SELECT COUNT(*) c FROM batch_revision WHERE batch_id IN ('full-a','full-b')").fetchone()['c'],2)
        report.decide(self.db,'student-1','C1',2026);report.decide(self.db,'student-1','C1',2026)
        self.assertEqual(self.db.execute('SELECT COUNT(*) c FROM result_report').fetchone()['c'],2)

    def test_idempotent_and_conflicting_retransmission(self):
        events=[source_event()]+self.full('full-1',1,[entry('C1'),entry('C2')],declared=2)
        first=catalog.ingest(self.db,events)
        second=catalog.ingest(self.db,events)
        self.assertEqual(second['imported'],0)
        self.assertEqual(first['releases'],second['releases'])
        third=catalog.ingest(self.db,[batch_page('page-full-1-1b','full-1',1,[entry('C1'),entry('C2')])])
        self.assertEqual(third['imported'],1)
        self.assertEqual(self.batch('full-1')['state'],'complete')
        self.assertEqual(second['releases'],third['releases'])
        catalog.ingest(self.db,[batch_page('page-full-1-1c','full-1',1,[entry('C1'),entry('C2',credits=5)])])
        self.assertEqual(self.batch('full-1')['reason'],'page_conflict')
        self.assertEqual(catalog.current(self.db,'C1',2026)['decision'],'untrusted')

    def test_source_revoke_recomputes_current_catalog(self):
        catalog.ingest(self.db,[source_event()]+self.full('full-1',1,[entry('C1'),entry('C2')],declared=2))
        self.assertEqual(catalog.current(self.db,'C1',2026)['decision'],'published')
        catalog.ingest(self.db,[{'event_id':'revoke-d','op':'source_revoke','source_id':'dept-d','event_time':'2026-05-01T00:00:00Z'}])
        self.assertEqual(catalog.current(self.db,'C1',2026)['decision'],'untrusted')
        catalog.ingest(self.db,[source_event(event_id='src-d-2',time='2026-05-02T00:00:00Z')])
        self.assertEqual(catalog.current(self.db,'C1',2026)['decision'],'published')

    def test_cli_import_catalog_export_and_report(self):
        from curriculum import __main__ as cli
        dbpath=str(Path(self.tmp.name)/'cli.sqlite')
        export=[source_event()]+self.full('full-1',1,[entry('C9')],declared=1)
        export_file=Path(self.tmp.name)/'export.json';export_file.write_text(json.dumps(export),encoding='utf-8')
        fixtures=ROOT/'fixtures'/'curriculum'
        self.assertEqual(cli.main(['import-catalog','--db',dbpath,'--input',str(fixtures/'catalog_good.json')]),0)
        self.assertEqual(cli.main(['import-export','--db',dbpath,'--input',str(export_file)]),0)
        self.assertEqual(cli.main(['import-prereq','--db',dbpath,'--input',str(fixtures/'prereq_good.json')]),0)
        self.assertEqual(cli.main(['import-credit','--db',dbpath,'--input',str(fixtures/'credit_good.json')]),0)
        self.assertEqual(cli.main(['report','--db',dbpath,'--student','student-1','--course','C3','--year','2026']),0)

if __name__=='__main__':unittest.main()
