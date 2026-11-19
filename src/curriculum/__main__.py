import argparse,json
from pathlib import Path
from . import catalog,prereq,credit,report,degree
from .store import connect

def main(argv=None):
    p=argparse.ArgumentParser(prog='curriculum');sp=p.add_subparsers(dest='command',required=True)
    for name in ('init','import-catalog','import-prereq','import-credit','import-degree','degree-report','report','demo'):
        q=sp.add_parser(name);q.add_argument('--db',default='data/demo.sqlite')
        if name.startswith('import-'):q.add_argument('--input',required=True)
        if name=='report':q.add_argument('--student',required=True);q.add_argument('--course',required=True);q.add_argument('--year',type=int,required=True)
        if name=='degree-report':q.add_argument('--student',required=True);q.add_argument('--program',required=True);q.add_argument('--year',type=int,required=True)
    a=p.parse_args(argv);db=connect(a.db)
    try:
        if a.command=='init':result={'initialized':True,'db':a.db}
        elif a.command.startswith('import-'):
            items=json.loads(Path(a.input).read_text(encoding='utf-8'))
            result={'import-catalog':catalog,'import-prereq':prereq,'import-credit':credit,'import-degree':degree}[a.command].ingest(db,items)
        elif a.command=='report':result=report.decide(db,a.student,a.course,a.year)
        elif a.command=='degree-report':result=degree.decide(db,a.student,a.program,a.year)
        else:
            root=Path(__file__).resolve().parents[2]
            for filename,mod in [('catalog_good.json',catalog),('prereq_good.json',prereq),('credit_good.json',credit)]:
                mod.ingest(db,json.loads((root/'fixtures'/'curriculum'/filename).read_text(encoding='utf-8')))
            result=report.decide(db,'student-1','C3',2026)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True));return 0
    except (KeyError,ValueError) as e:print(json.dumps({'error':str(e)},ensure_ascii=False));return 2
    finally:db.close()

if __name__=='__main__':raise SystemExit(main())
