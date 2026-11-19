"""Authority, version conflict and historical course releases."""
import hashlib,json
from .sources import record,canonical
from .store import rows

DECL_FIELDS=('source_id','department','year','kind','version','page_count','declared_courses')


def page_digest(courses):
    """Digest one manifest page exactly as the exporting department signed it."""
    return hashlib.sha256(canonical(courses).encode()).hexdigest()


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
            elif op=='source_revoke':
                if record(db,e,'catalog_source_revoke',e['source_id']):
                    db.execute('DELETE FROM catalog_source WHERE source_id=?',(e['source_id'],))
                    count+=1
            elif op=='course':
                if e['credits']<=0:raise ValueError('credits must be positive')
                if record(db,e,'course',e['course_id']):
                    db.execute('INSERT INTO course_revision VALUES (?,?,?,?,?,?,?,?)',
                               (e['event_id'],e['course_id'],e['department'],e['source_id'],e['year'],e['version'],e['credits'],e['status']))
                    count+=1
            elif op=='export_batch':
                if declare(db,e):count+=1
            elif op=='export_page':
                if receive_page(db,e):count+=1
            else:raise ValueError(f'unknown catalog op {op}')
        # Recompute every projection from source events: adjudicate batches,
        # rebuild batch-derived revisions, then re-release every touched pair.
        for b in rows(db,'SELECT DISTINCT batch_id FROM export_batch_decl'):adjudicate(db,b['batch_id'])
        derive(db)
        pairs=rows(db,'SELECT DISTINCT course_id,year FROM revision_all UNION SELECT DISTINCT course_id,year FROM catalog_release')
        for r in pairs:release(db,r['course_id'],r['year'])
    return {'imported':count,'releases':rows(db,'SELECT * FROM catalog_release'),'batches':rows(db,'SELECT * FROM export_batch')}


def declare(db,e):
    """Record a department export commitment (complete catalog or change patch)."""
    if e['kind'] not in ('complete','patch'):raise ValueError('unknown export kind')
    if e['page_count']<1:raise ValueError('page_count must be positive')
    if e['declared_courses']<0:raise ValueError('declared_courses must be non-negative')
    if not record(db,e,'export_batch',e['batch_id']):return False
    db.execute('INSERT INTO export_batch_decl VALUES (?,?,?)',(e['batch_id'],e['event_id'],canonical({k:e[k] for k in DECL_FIELDS})))
    return True


def receive_page(db,e):
    """Record one manifest page; identical pages may arrive repeatedly."""
    if e['page']<1:raise ValueError('page must be positive')
    courses=e['courses']
    if not isinstance(courses,list):raise ValueError('page courses must be a list')
    for c in courses:
        if c['credits']<=0:raise ValueError('credits must be positive')
        if c.get('status','open') not in ('open','stopped'):raise ValueError('unknown course status')
    if not record(db,e,'export_page',e['batch_id']):return False
    db.execute('INSERT INTO export_page VALUES (?,?,?,?,?)',(e['batch_id'],e['page'],e['event_id'],e['page_digest'],canonical(courses)))
    return True


def adjudicate(db,batch_id):
    """Decide whether a declared export batch is collected and internally consistent."""
    decls=rows(db,'SELECT * FROM export_batch_decl WHERE batch_id=? ORDER BY declare_event',(batch_id,))
    if not decls:return None
    first=json.loads(decls[0]['body'])
    state,reason,detail='pending','awaiting_pages',{}
    if len({d['body'] for d in decls})>1:
        state,reason='failed','declaration_conflict'
    else:
        pages=rows(db,'SELECT * FROM export_page WHERE batch_id=? ORDER BY page,page_event',(batch_id,))
        by_page={}
        for p in pages:by_page.setdefault(p['page'],[]).append(p)
        unique=[by_page[n][0] for n in sorted(by_page)]
        clashed=sorted(n for n,g in by_page.items() if len({p['body'] for p in g})>1)
        bad_digest=sorted({p['page'] for p in pages if p['digest']!=page_digest(json.loads(p['body']))})
        if clashed:
            state,reason='failed','page_conflict';detail={'pages':clashed}
        elif bad_digest:
            state,reason='failed','page_digest_mismatch';detail={'pages':bad_digest}
        else:
            extra=sorted(n for n in by_page if n<1 or n>first['page_count'])
            missing=[n for n in range(1,first['page_count']+1) if n not in by_page]
            if extra:state,reason='failed','unexpected_page';detail={'pages':extra}
            elif missing:state,reason='pending','awaiting_pages';detail={'missing':missing}
            else:
                seen={};total=0;duplicate=None;conflict=None
                for p in unique:
                    for c in json.loads(p['body']):
                        total+=1;key=c['course_id'];body=canonical(c)
                        if key in seen:
                            if seen[key]!=body:conflict=conflict or key
                            else:duplicate=duplicate or key
                        else:seen[key]=body
                if conflict:state,reason='failed','cross_page_conflict';detail={'course_id':conflict}
                elif duplicate:state,reason='failed','duplicate_course';detail={'course_id':duplicate}
                elif total!=first['declared_courses']:
                    state,reason='failed','count_mismatch';detail={'declared':first['declared_courses'],'received':total}
                else:state,reason='complete','all_pages_verified'
    db.execute('INSERT INTO export_batch VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(batch_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,detail=excluded.detail',
               (batch_id,first['source_id'],first['department'],first['year'],first['kind'],first['version'],first['page_count'],first['declared_courses'],state,reason,canonical(detail),decls[0]['declare_event']))
    return {'batch_id':batch_id,'state':state,'reason':reason,'detail':detail}


def batch_entries(db,batch_id):
    """First verified content of each page of a batch, as (page, event, course) rows."""
    seen={}
    for p in rows(db,'SELECT * FROM export_page WHERE batch_id=? ORDER BY page,page_event',(batch_id,)):
        seen.setdefault(p['page'],p)
    return [(n,seen[n]['page_event'],c) for n in sorted(seen) for c in json.loads(seen[n]['body'])]


def derive(db):
    """Rebuild batch-derived revisions from stored batch and page evidence.

    Listed courses become revisions at the batch version. A complete batch
    additionally turns courses of the previous effective complete batch that it
    omits into sourced stopped candidates; a patch never does.
    """
    db.execute('DELETE FROM batch_revision')
    done=rows(db,"SELECT * FROM export_batch WHERE state='complete' ORDER BY department,year,version,batch_id")
    entries={b['batch_id']:batch_entries(db,b['batch_id']) for b in done}
    for b in done:
        mine=entries[b['batch_id']]
        for page,event,c in mine:
            db.execute('INSERT INTO batch_revision VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (f"{b['batch_id']}:{c['course_id']}",b['batch_id'],page,c['course_id'],b['department'],b['source_id'],b['year'],b['version'],c['credits'],c.get('status','open'),event))
        if b['kind']!='complete':continue
        previous=[g for g in done if g['kind']=='complete' and (g['department'],g['year'])==(b['department'],b['year']) and g['version']<b['version']]
        if not previous:continue
        top=max(g['version'] for g in previous)
        covered={}
        for g in previous:
            if g['version']!=top:continue
            for _,_,c in entries[g['batch_id']]:covered.setdefault(c['course_id'],c['credits'])
        listed={c['course_id'] for _,_,c in mine}
        for absent in sorted(set(covered)-listed):
            db.execute('INSERT INTO batch_revision VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (f"{b['batch_id']}:~{absent}",b['batch_id'],None,absent,b['department'],b['source_id'],b['year'],b['version'],covered[absent],'stopped',b['declare_event']))


def release(db,course_id,year):
    items=rows(db,'SELECT c.* FROM revision_all c LEFT JOIN catalog_source s ON s.source_id=c.source_id WHERE c.course_id=? AND c.year=? AND s.department=c.department AND CAST(s.valid_from AS INTEGER)<=c.year AND CAST(s.valid_until AS INTEGER)>=c.year ORDER BY c.version DESC,c.revision_id DESC',(course_id,year))
    if not items:decision='untrusted';reason='no_authorized_source';chosen=None
    else:
        top=[i for i in items if i['version']==items[0]['version']]
        if len({(i['credits'],i['status']) for i in top})>1:
            decision='disputed';reason='same_version_conflict';chosen=None
        else:
            chosen=top[0]['revision_id'];decision='stopped' if top[0]['status']=='stopped' else 'published';reason='authorized_latest'
    db.execute('INSERT INTO catalog_release VALUES (?,?,?,?,?) ON CONFLICT(course_id,year) DO UPDATE SET event_id=excluded.event_id,decision=excluded.decision,reason=excluded.reason',
               (course_id,year,chosen,decision,reason))
    return {'course_id':course_id,'year':year,'decision':decision,'reason':reason,'event_id':chosen}


def current(db,course_id,year):
    r=db.execute('SELECT r.*,c.credits,c.status,c.batch_id,c.page,c.origin_event FROM catalog_release r LEFT JOIN revision_all c ON c.revision_id=r.event_id WHERE r.course_id=? AND r.year=?',(course_id,year)).fetchone()
    return dict(r) if r else {'course_id':course_id,'year':year,'decision':'missing','reason':'no_catalog'}
