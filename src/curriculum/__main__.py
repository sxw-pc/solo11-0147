import argparse,json
from pathlib import Path
from . import catalog,prereq,credit,report,outline,assessment
from .store import connect

def main(argv=None):
    p=argparse.ArgumentParser(prog='curriculum');sp=p.add_subparsers(dest='command',required=True)
    for name in ('init','import-catalog','import-prereq','import-credit','import-outline','import-assessment','import-calibration','outline-report','assessment-report','batch-review','review-history','report','demo'):
        q=sp.add_parser(name);q.add_argument('--db',default='data/demo.sqlite')
        if name.startswith('import-'):q.add_argument('--input',required=True)
        if name=='report':q.add_argument('--student',required=True);q.add_argument('--course',required=True);q.add_argument('--year',type=int,required=True)
        if name=='outline-report':q.add_argument('--course',required=True);q.add_argument('--year',type=int,required=True)
        if name in ('assessment-report','batch-review','review-history'):
            q.add_argument('--course',required=True);q.add_argument('--year',type=int,required=True);q.add_argument('--objective',required=True)
        if name=='assessment-report':q.add_argument('--student',required=True)
        if name=='batch-review':q.add_argument('--tolerance',type=int,required=True)
    a=p.parse_args(argv);db=connect(a.db)
    try:
        if a.command=='init':result={'initialized':True,'db':a.db}
        elif a.command=='import-calibration':
            result=assessment.calibrate(db,json.loads(Path(a.input).read_text(encoding='utf-8')))
        elif a.command.startswith('import-'):
            items=json.loads(Path(a.input).read_text(encoding='utf-8'))
            result={'import-catalog':catalog,'import-prereq':prereq,'import-credit':credit,'import-outline':outline,'import-assessment':assessment}[a.command].ingest(db,items)
        elif a.command=='report':result=report.decide(db,a.student,a.course,a.year)
        elif a.command=='outline-report':result=outline.current(db,a.course,a.year)
        elif a.command=='assessment-report':result=assessment.decide(db,a.student,a.course,a.year,a.objective)
        elif a.command=='batch-review':
            result=assessment.review_batch(db,a.course,a.year,a.objective,a.tolerance)
            db.commit()
        elif a.command=='review-history':result=assessment.review_history(db,a.course,a.year,a.objective)
        else:
            root=Path(__file__).resolve().parents[2]
            for filename,mod in [('catalog_good.json',catalog),('prereq_good.json',prereq),('credit_good.json',credit)]:
                mod.ingest(db,json.loads((root/'fixtures'/'curriculum'/filename).read_text(encoding='utf-8')))
            result=report.decide(db,'student-1','C3',2026)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True));return 0
    except (KeyError,ValueError) as e:print(json.dumps({'error':str(e)},ensure_ascii=False));return 2
    finally:db.close()

if __name__=='__main__':raise SystemExit(main())
