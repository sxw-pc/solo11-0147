"""Authority, version conflict and historical course releases."""
from .sources import record
from .store import rows


def ingest(db,events):
    count=0
    with db:
        for e in events:
            op=e['op']
            if op=='source':
                if record(db,e,'catalog_source',e['source_id']):
                    db.execute('INSERT INTO catalog_source VALUES (?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET department=excluded.department,valid_from=excluded.valid_from,valid_until=excluded.valid_until,source_event=excluded.source_event',
                               (e['source_id'],e['department'],str(e['valid_from']),str(e['valid_until']),e['event_id']))
                    count+=1
            elif op=='course':
                if e['credits']<=0:raise ValueError('credits must be positive')
                if record(db,e,'course',e['course_id']):
                    db.execute('INSERT INTO course_revision VALUES (?,?,?,?,?,?,?,?)',
                               (e['event_id'],e['course_id'],e['department'],e['source_id'],e['year'],e['version'],e['credits'],e['status']))
                    count+=1
            else:raise ValueError(f'unknown catalog op {op}')
        for r in rows(db,'SELECT DISTINCT course_id,year FROM course_revision'):
            release(db,r['course_id'],r['year'])
    return {'imported':count,'releases':rows(db,'SELECT * FROM catalog_release')}


def release(db,course_id,year):
    items=rows(db,'SELECT c.* FROM course_revision c LEFT JOIN catalog_source s ON s.source_id=c.source_id WHERE c.course_id=? AND c.year=? AND s.department=c.department AND CAST(s.valid_from AS INTEGER)<=c.year AND CAST(s.valid_until AS INTEGER)>=c.year ORDER BY c.version DESC,c.event_id DESC',(course_id,year))
    if not items:decision='untrusted';reason='no_authorized_source';chosen=None
    else:
        top=[i for i in items if i['version']==items[0]['version']]
        if len({(i['credits'],i['status']) for i in top})>1:
            decision='disputed';reason='same_version_conflict';chosen=None
        else:
            chosen=top[0]['event_id'];decision='stopped' if top[0]['status']=='stopped' else 'published';reason='authorized_latest'
    db.execute('INSERT INTO catalog_release VALUES (?,?,?,?,?) ON CONFLICT(course_id,year) DO UPDATE SET event_id=excluded.event_id,decision=excluded.decision,reason=excluded.reason',
               (course_id,year,chosen,decision,reason))
    return {'course_id':course_id,'year':year,'decision':decision,'reason':reason,'event_id':chosen}


def current(db,course_id,year):
    r=db.execute('SELECT r.*,c.credits,c.status FROM catalog_release r LEFT JOIN course_revision c ON c.event_id=r.event_id WHERE r.course_id=? AND r.year=?',(course_id,year)).fetchone()
    return dict(r) if r else {'course_id':course_id,'year':year,'decision':'missing','reason':'no_catalog'}
