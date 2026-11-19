"""Immutable source event capture."""
import json,hashlib

def canonical(obj):return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def record(db,event,kind,object_id):
    body=canonical(event)
    digest=hashlib.sha256(body.encode()).hexdigest();event_id=event['event_id']
    old=db.execute('SELECT sha256 FROM evidence WHERE event_id=?',(event_id,)).fetchone()
    if old:
        if old['sha256']!=digest:raise ValueError(f'conflicting event id: {event_id}')
        return False
    db.execute('INSERT INTO evidence VALUES (?,?,?,?,?,?)',(event_id,kind,object_id,event['event_time'],digest,body))
    return True
