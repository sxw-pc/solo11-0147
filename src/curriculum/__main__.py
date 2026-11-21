import argparse, json
from pathlib import Path
from . import catalog, prereq, credit, report, survey
from .store import connect


def main(argv=None):
    p = argparse.ArgumentParser(prog='curriculum')
    sp = p.add_subparsers(dest='command', required=True)
    for name in ('init', 'import-catalog', 'import-prereq', 'import-credit',
                 'import-survey', 'survey-report', 'survey-publish',
                 'survey-public', 'survey-audit', 'report', 'demo'):
        q = sp.add_parser(name)
        q.add_argument('--db', default='data/demo.sqlite')
        if name.startswith('import-'):
            q.add_argument('--input', required=True)
        if name == 'report':
            q.add_argument('--student', required=True)
            q.add_argument('--course', required=True)
            q.add_argument('--year', type=int, required=True)
        if name in ('survey-report', 'survey-publish', 'survey-public', 'survey-audit'):
            q.add_argument('--course', required=True)
            q.add_argument('--year', type=int, required=True)
            q.add_argument('--form', required=True)
        if name == 'survey-publish':
            q.add_argument('--group', action='append', required=True,
                           help='comma-separated response tokens; repeatable')
            q.add_argument('--version', type=int)
            q.add_argument('--precision', type=int, default=survey.DEFAULT_PRECISION)
            q.add_argument('--no-total', action='store_true',
                           help='publish only count and rounded mean, not the total')
    a = p.parse_args(argv)
    db = connect(a.db)
    try:
        if a.command == 'init':
            result = {'initialized': True, 'db': a.db}
        elif a.command.startswith('import-'):
            items = json.loads(Path(a.input).read_text(encoding='utf-8'))
            result = {'import-catalog': catalog, 'import-prereq': prereq,
                      'import-credit': credit, 'import-survey': survey}[a.command].ingest(db, items)
        elif a.command == 'report':
            result = report.decide(db, a.student, a.course, a.year)
        elif a.command == 'survey-report':
            result = survey.decide(db, a.course, a.year, a.form)
        elif a.command == 'survey-publish':
            result = survey.publish(db, a.course, a.year, a.form, a.group,
                                    version=a.version, precision=a.precision,
                                    publish_total=not a.no_total)
        elif a.command == 'survey-public':
            result = survey.public_report(db, a.course, a.year, a.form)
        elif a.command == 'survey-audit':
            result = survey.audit(db, a.course, a.year, a.form)
        else:
            root = Path(__file__).resolve().parents[2]
            for filename, mod in [('catalog_good.json', catalog),
                                  ('prereq_good.json', prereq),
                                  ('credit_good.json', credit)]:
                mod.ingest(db, json.loads((root / 'fixtures' / 'curriculum' / filename).read_text(encoding='utf-8')))
            result = report.decide(db, 'student-1', 'C3', 2026)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (KeyError, ValueError) as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False))
        return 2
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
