"""Zonal risk and product release decisions over a reprojection run."""
import json
from .store import connect
from .engine import audit

def register_zone(path,name,cells,min_coverage,limit_value):
    if not name or not cells or not 0<min_coverage<=1:raise ValueError('bad_zone_contract')
    keys=[(int(r),int(c)) for r,c in cells]
    if len(set(keys))!=len(keys):raise ValueError('duplicate_zone_cell')
    body=json.dumps(keys,sort_keys=True)
    db=connect(path)
    try:
        with db:
            old=db.execute('SELECT * FROM zone WHERE id=?',(name,)).fetchone()
            if old and (old['cell_ids'],old['min_coverage'],old['limit_value'])!=(body,min_coverage,limit_value):raise ValueError('zone_version_conflict')
            db.execute('INSERT OR IGNORE INTO zone VALUES (?,?,?,?)',(name,body,min_coverage,limit_value))
    finally:db.close()

def evaluate_zone(path,run_id,name):
    db=connect(path)
    try:
        with db:
            zone=db.execute('SELECT * FROM zone WHERE id=?',(name,)).fetchone()
            run=db.execute('SELECT status FROM run WHERE id=?',(run_id,)).fetchone()
            if not zone or not run:raise ValueError('unknown_zone_or_run')
            ids=json.loads(zone['cell_ids']);values=[];covers=[];evidence=[]
            for r,c in ids:
                row=db.execute('SELECT * FROM cell WHERE run_id=? AND row=? AND col=?',(run_id,r,c)).fetchone()
                if not row:raise ValueError('zone_cell_outside_grid')
                covers.append(row['coverage']);evidence.append([r,c,row['value']])
                if row['value'] is not None:values.append(row['value'])
            coverage=sum(min(max(x,0),1) for x in covers)/len(ids)
            measured=sum(values)/len(values) if values else None
            if run['status']!='已核验' or coverage<zone['min_coverage'] or len(values)!=len(ids):status='证据不足'
            elif measured>zone['limit_value']:status='超限'
            else:status='正常'
            db.execute('INSERT INTO zone_decision VALUES (?,?,?,?,?,?) ON CONFLICT(run_id,zone_id) DO UPDATE SET status=excluded.status,measured=excluded.measured,coverage=excluded.coverage,evidence=excluded.evidence',
                       (run_id,name,status,measured,coverage,json.dumps(evidence)))
            return {'status':status,'measured':measured,'coverage':coverage}
    finally:db.close()

def decide_release(path,request_id,run_id,zones):
    if not zones or len(set(zones))!=len(zones):raise ValueError('bad_release_scope')
    # Re-evaluate the source and output before making the product decision.
    audit(path,run_id)
    db=connect(path)
    try:
        with db:
            run=db.execute('SELECT status FROM run WHERE id=?',(run_id,)).fetchone()
            if not run:raise ValueError('unknown_run')
            old=db.execute('SELECT * FROM release_request WHERE id=?',(request_id,)).fetchone()
            frozen=json.dumps(sorted(zones))
            if old and (old['run_id'],old['zone_ids'])!=(run_id,frozen):raise ValueError('release_identity_conflict')
            decisions=[]
            for name in zones:
                row=db.execute('SELECT status FROM zone_decision WHERE run_id=? AND zone_id=?',(run_id,name)).fetchone()
                decisions.append(row['status'] if row else '未裁定')
            if run['status']!='已核验' or any(x in {'证据不足','未裁定'} for x in decisions):status,reason='暂缓','evidence_gap'
            elif any(x=='超限' for x in decisions):status,reason='拒发','risk_exceeded'
            else:status,reason='准予发布','all_zones_safe'
            db.execute('INSERT INTO release_request(id,run_id,zone_ids,status,reason) VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,reason=excluded.reason',
                       (request_id,run_id,frozen,status,reason))
            return {'status':status,'reason':reason,'zone_decisions':decisions}
    finally:db.close()
