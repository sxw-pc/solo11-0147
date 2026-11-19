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

if __name__=='__main__':unittest.main()
