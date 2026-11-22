import hashlib
import json
import math
import os
from pathlib import Path
import numpy as np
import rasterio
from rasterio.transform import Affine, from_bounds, rowcol
from rasterio.warp import transform, reproject, Resampling
from .store import connect
from . import geometry

CORRECTION_MODELS = ('global_affine', 'local_mesh')
BOUNDARY_POLICIES = ('reject',)
AREA_SUBSAMPLES = 4
NODATA = -9999.0


def sha(data):return hashlib.sha256(data).hexdigest()
def inspect(db_path,path,unit='unit'):
    path=Path(path);raw=path.read_bytes()
    with rasterio.open(path) as src:
        if not src.crs or src.width<=0 or src.height<=0 or abs(src.transform.determinant)<1e-15:raise ValueError('bad_source_grid')
        info={'width':src.width,'height':src.height,'crs':str(src.crs),'nodata':src.nodata,'unit':unit,'hash':sha(raw)}
    db=connect(db_path)
    with db:
        db.execute('INSERT OR IGNORE INTO source(path,hash,width,height,crs,nodata,unit) VALUES (?,?,?,?,?,?,?)',(str(path),info['hash'],info['width'],info['height'],info['crs'],info['nodata'],unit))
    db.close();return info

def plan(db_path,contract):
    needed={'crs','bounds','width','height','kind','method','min_coverage'}
    if not needed.issubset(contract):raise ValueError('missing_contract')
    x0,y0,x1,y1=contract['bounds'];w=int(contract['width']);h=int(contract['height'])
    if w<=0 or h<=0 or x1<=x0 or y1<=y0 or not 0<=contract['min_coverage']<=1:raise ValueError('bad_grid')
    methods={'category':{'nearest'},'continuous':{'bilinear'},'extensive':{'area'}}
    if contract['method'] not in methods.get(contract['kind'],set()):raise ValueError('method_kind_conflict')
    try:rasterio.crs.CRS.from_string(contract['crs'])
    except Exception as exc:raise ValueError('bad_crs') from exc
    _validate_correction_contract(contract)
    body=json.dumps(contract,sort_keys=True,separators=(',',':'))
    db=connect(db_path)
    with db:db.execute('INSERT OR IGNORE INTO contract(hash,body) VALUES (?,?)',(sha(body.encode()),body))
    db.close();return sha(body.encode())

def _validate_correction_contract(contract):
    survey_id=contract.get('survey_id')
    model=contract.get('correction_model')
    limit=contract.get('control_residual_limit')
    if survey_id is None and model is None and limit is None:
        if contract.get('mesh_boundary_policy','reject')!='reject':
            raise ValueError('correction_contract_invalid')
        return
    if not survey_id or model not in CORRECTION_MODELS:
        raise ValueError('correction_contract_invalid')
    if not isinstance(limit,(int,float)) or isinstance(limit,bool) or not math.isfinite(limit) or limit<=0:
        raise ValueError('correction_contract_invalid')
    if contract.get('mesh_boundary_policy','reject') not in BOUNDARY_POLICIES:
        raise ValueError('correction_contract_invalid')

def map_cell(src,target_transform,target_crs,r,c):
    x,y=target_transform @ (c+0.5,r+0.5)
    xs,ys=transform(target_crs,src.crs,[x],[y]);sr,sc=rowcol(src.transform,xs[0],ys[0],op=float)
    return sr,sc


# --------------------------------------------------------------------------- #
# Control-point corrected runs
# --------------------------------------------------------------------------- #
def _load_survey(db, survey_id):
    survey = db.execute('SELECT * FROM control_survey WHERE id=?', (survey_id,)).fetchone()
    if not survey:
        raise geometry.GeometryError('unknown_control_survey', survey_id)
    points = [dict(row) for row in db.execute(
        'SELECT point_id AS id,source_row,source_col,target_x,target_y,weight '
        'FROM control_point WHERE survey_id=? ORDER BY point_id', (survey_id,))]
    if len(points) < 3:
        raise geometry.GeometryError('control_points_insufficient')
    return dict(survey), points


def _record_rejected_fit(db_path, survey_id, model, limit, policy, exc):
    """Persist why a frozen control set could not produce a publishable run."""
    payload = getattr(exc, 'payload', None) or {}
    records = payload.get('records', [])
    coef = {'model': model, 'rejected': True}
    db = connect(db_path)
    try:
        survey = db.execute('SELECT digest FROM control_survey WHERE id=?',
                            (survey_id,)).fetchone()
        digest = survey['digest'] if survey else None
        weights = {p['point_id']: p['weight'] for p in db.execute(
            'SELECT point_id,weight FROM control_point WHERE survey_id=?',
            (survey_id,))}
        with db:
            fit_id = db.execute(
                'INSERT INTO control_fit(run_id,survey_id,model,residual_limit,'
                'boundary_policy,observation_digest,coef,status,error_code,error_detail) '
                'VALUES (NULL,?,?,?,?,?,?,?,?,?)',
                (survey_id, model, limit, policy, digest,
                 json.dumps(coef, sort_keys=True), '拒绝', exc.code,
                 exc.detail)).lastrowid
            db.executemany(
                'INSERT INTO control_fit_point(fit_id,point_id,predicted_x,predicted_y,'
                'residual,weight,accepted,reject_reason) VALUES (?,?,?,?,?,?,?,?)',
                [(fit_id, rec['point_id'], rec.get('predicted_x'), rec.get('predicted_y'),
                  rec.get('residual'), weights.get(rec['point_id'], 0.0), 0,
                  rec.get('reason') or exc.code)
                 for rec in records])
    finally:
        db.close()


def _target_cell_centers(contract, target_transform, survey_crs):
    rows, cols = np.meshgrid(np.arange(contract['height']),
                             np.arange(contract['width']), indexing='ij')
    xs, ys = target_transform @ (cols + 0.5, rows + 0.5)
    sx, sy = geometry.crs_transform(contract['crs'], survey_crs,
                                    xs.ravel().tolist(), ys.ravel().tolist())
    return np.array(sx).reshape(xs.shape), np.array(sy).reshape(ys.shape)


def _target_cell_area(contract, target_transform, survey_crs, r, c):
    corners = [(c, r), (c + 1, r), (c + 1, r + 1), (c, r + 1)]
    gx = [target_transform @ (cc, rr) for cc, rr in corners]
    sx, sy = geometry.crs_transform(contract['crs'], survey_crs,
                                    [p[0] for p in gx], [p[1] for p in gx])
    area = 0.0
    for i in range(4):
        j = (i + 1) % 4
        area += sx[i] * sy[j] - sx[j] * sy[i]
    return abs(area) * 0.5


def _sample_nearest(source, valid, srow, scol):
    ir, ic = int(round(srow)), int(round(scol))
    if not (0 <= ir < source.shape[0] and 0 <= ic < source.shape[1]):
        return None, 0.0, 'source_outside'
    if not valid[ir, ic]:
        return None, 0.0, 'source_nodata'
    return float(source[ir, ic]), 1.0, None


def _sample_bilinear(source, valid, srow, scol):
    r0 = int(math.floor(srow)); c0 = int(math.floor(scol))
    r1, c1 = r0 + 1, c0 + 1
    if r1 < 0 or c1 < 0 or r0 >= source.shape[0] or c0 >= source.shape[1]:
        return None, 0.0, 'source_outside'
    fr, fc = srow - r0, scol - c0
    weights = {(r0, c0): (1 - fr) * (1 - fc), (r0, c1): (1 - fr) * fc,
               (r1, c0): fr * (1 - fc), (r1, c1): fr * fc}
    num = den = 0.0
    for (rr, cc), wgt in weights.items():
        if 0 <= rr < source.shape[0] and 0 <= cc < source.shape[1] and valid[rr, cc]:
            num += wgt * source[rr, cc]
            den += wgt
    if den <= 0:
        return None, 0.0, 'source_nodata'
    return num / den, den, None


def _run_corrected(db_path, info, contract, out, target_transform, db, src_id, cid,
                   survey_id, model_name, residual_limit, boundary_policy):
    survey, points = _load_survey(db, survey_id)
    if survey['source_hash'] != info['hash']:
        raise ValueError('control_source_hash_mismatch')
    geometry.ensure_crs_transformable(survey['target_crs'], contract['crs'])
    weight_by_id = {p['id']: p['weight'] for p in points}
    try:
        model, fit_records = geometry.fit_model(model_name, points, residual_limit)
    except geometry.GeometryError as exc:
        db.close()
        _record_rejected_fit(db_path, survey_id, model_name,
                             residual_limit, boundary_policy, exc)
        raise ValueError(exc.code) from exc

    with rasterio.open(info['_source_path']) as src:
        source = src.read(1).astype('float64')
        valid = np.isfinite(source)
        if src.nodata is not None:
            valid &= source != src.nodata
        profile = src.profile.copy()

    try:
        centers_x, centers_y = _target_cell_centers(contract, target_transform,
                                                    survey['target_crs'])
    except geometry.GeometryError as exc:
        db.close()
        _record_rejected_fit(db_path, survey_id, model_name,
                             residual_limit, boundary_policy, exc)
        raise ValueError(exc.code) from exc
    shape = (contract['height'], contract['width'])
    result = np.full(shape, NODATA, dtype='float64')
    provenance, basis_rows, issues = [], [], []
    for r in range(shape[0]):
        for c in range(shape[1]):
            tx, ty = float(centers_x[r, c]), float(centers_y[r, c])
            try:
                located = model.inverse(tx, ty)
            except geometry.GeometryError as exc:
                provenance.append((r, c, None, 0.0, None, None, 0.0))
                basis_rows.append((r, c, '不可定位', tx, ty, None, None,
                                   json.dumps({'model': model_name}, sort_keys=True),
                                   exc.code))
                issues.append((r, c, 'unlocatable', exc.code))
                continue
            if model_name == 'local_mesh':
                srow, scol, qi, qj, u, v = located
                detail = {'model': model_name, 'kind': 'mesh_inverse',
                          'quad_row': qi, 'quad_col': qj, 'u': u, 'v': v}
            else:
                srow, scol = located
                detail = {'model': model_name, 'kind': 'affine_inverse'}
            method = contract['method']
            if method == 'nearest':
                value, coverage, reason = _sample_nearest(source, valid, srow, scol)
            elif method == 'bilinear':
                value, coverage, reason = _sample_bilinear(source, valid, srow, scol)
            else:
                value, coverage, reason = _sample_area(
                    contract, target_transform, survey['target_crs'], model,
                    source, valid, r, c)
            good = coverage >= contract['min_coverage'] and coverage > 0
            if good:
                result[r, c] = value
            else:
                issues.append((r, c, 'coverage_low',
                               reason or f'{coverage:.3f}'))
            provenance.append((r, c, value if good else None, coverage,
                               float(srow), float(scol), coverage))
            basis_rows.append((r, c, '可定位' if good else '覆盖不足',
                               tx, ty, float(srow), float(scol),
                               json.dumps(detail, sort_keys=True), reason))

    profile.update(driver='GTiff', width=shape[1], height=shape[0], count=1,
                   dtype='float64', crs=contract['crs'],
                   transform=target_transform, nodata=NODATA)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix('.tmp.tif')
    if out.exists():
        raise ValueError('output_exists')
    with rasterio.open(tmp, 'w', **profile) as dst:
        dst.write(result, 1)
    published = False
    try:
        with db:
            rid = db.execute(
                "INSERT INTO run(source_id,contract_id,status,method) VALUES (?,?,?,?)",
                (src_id, cid, '采样中', contract['method'])).lastrowid
            fit_id = db.execute(
                'INSERT INTO control_fit(run_id,survey_id,model,residual_limit,'
                'boundary_policy,observation_digest,coef,status,error_code,error_detail) '
                'VALUES (?,?,?,?,?,?,?,?,?,?)',
                (rid, survey_id, model_name, residual_limit, boundary_policy,
                 survey['digest'], json.dumps(model.to_dict(), sort_keys=True),
                 '采用', None, None)).lastrowid
            db.executemany(
                'INSERT INTO control_fit_point(fit_id,point_id,predicted_x,predicted_y,'
                'residual,weight,accepted,reject_reason) VALUES (?,?,?,?,?,?,?,?)',
                [(fit_id, rec['point_id'], rec['predicted_x'], rec['predicted_y'],
                  rec['residual'], weight_by_id[rec['point_id']],
                  1 if rec['accepted'] else 0, rec['reason'])
                 for rec in fit_records])
            db.executemany('INSERT INTO cell VALUES (?,?,?,?,?,?,?,?)',
                           [(rid, *x) for x in provenance])
            db.executemany(
                'INSERT INTO cell_basis(run_id,row,col,status,target_x,target_y,'
                'source_row,source_col,basis,reason) VALUES (?,?,?,?,?,?,?,?,?,?)',
                [(rid, *x) for x in basis_rows])
            for rr, cc, code, detail in issues:
                db.execute('INSERT INTO issue(run_id,row,col,code,detail) VALUES (?,?,?,?,?)',
                           (rid, rr, cc, code, str(detail)))
            os.replace(tmp, out)
            published = True
            db.execute('INSERT INTO output VALUES (?,?,?)',
                       (rid, str(out), sha(out.read_bytes())))
            good_cells = all(x[2] is not None for x in provenance)
            db.execute("UPDATE run SET status=?,ended=CURRENT_TIMESTAMP WHERE id=?",
                       ('待核' if not good_cells else '已核验', rid))
    except Exception:
        tmp.unlink(missing_ok=True)
        if published:
            out.unlink(missing_ok=True)
        raise
    return rid


def _sample_area(contract, target_transform, survey_crs, model,
                 source, valid, r, c):
    """Supersampled extensive sampling with Jacobian area weighting.

    Returns (amount, coverage_fraction, reason). The amount integrates the
    source density over the target cell footprint (GDAL ``sum`` convention);
    coverage is separately the located fraction of that footprint with valid
    source pixels, so a partial hole never inflates either number.
    """
    k = AREA_SUBSAMPLES
    offsets = [(i + 0.5) / k for i in range(k)]
    gx, gy = [], []
    for u in offsets:
        for v in offsets:
            x, y = target_transform @ (c + v, r + u)
            gx.append(x); gy.append(y)
    sx, sy = geometry.crs_transform(contract['crs'], survey_crs, gx, gy)
    area = _target_cell_area(contract, target_transform, survey_crs, r, c)
    if not math.isfinite(area) or area <= 0:
        return None, 0.0, 'target_area_invalid'
    located_area = 0.0
    valid_area = 0.0
    amount = 0.0
    located_any = False
    for x, y in zip(sx, sy):
        try:
            located = model.inverse(float(x), float(y))
        except geometry.GeometryError:
            continue
        located_any = True
        srow, scol = located[0], located[1]
        jac = model.jacobian(srow, scol)
        if not math.isfinite(jac) or jac <= 0:
            continue
        patch_area = jac / (k * k)
        located_area += patch_area
        # Point-in-pixel containment (floor), matching GDAL's area convention:
        # a sub-sample anywhere inside a source pixel belongs to that pixel,
        # so edge rows/cols are never lost to nearest-centre rounding.
        ir, ic = int(math.floor(srow)), int(math.floor(scol))
        if 0 <= ir < source.shape[0] and 0 <= ic < source.shape[1] and valid[ir, ic]:
            valid_area += patch_area
            amount += source[ir, ic] * patch_area
    if not located_any:
        return None, 0.0, 'unlocatable'
    coverage = min(located_area / area, 1.0)
    if valid_area <= 0:
        return None, coverage, 'source_nodata'
    return amount, coverage, None


def run(db_path,source_path,contract,out):
    source_path=Path(source_path);out=Path(out);info=inspect(db_path,source_path);ch=plan(db_path,contract)
    info['_source_path']=source_path
    db=connect(db_path)
    src_id=db.execute('SELECT id FROM source WHERE hash=?',(info['hash'],)).fetchone()['id']
    cid=db.execute('SELECT id FROM contract WHERE hash=?',(ch,)).fetchone()['id']
    transform_out=from_bounds(*contract['bounds'],contract['width'],contract['height'])
    survey_id=contract.get('survey_id')
    if survey_id is not None:
        rid=_run_corrected(db_path,info,contract,out,transform_out,db,src_id,cid,
                           survey_id,contract['correction_model'],
                           float(contract['control_residual_limit']),
                           contract.get('mesh_boundary_policy','reject'))
        db.close()
        audit(db_path,rid)
        return rid
    method=contract['method'];resampling={'nearest':Resampling.nearest,'bilinear':Resampling.bilinear,'area':Resampling.sum}[method]
    with rasterio.open(source_path) as src:
        source=src.read(1).astype('float64');valid=np.isfinite(source)
        if src.nodata is not None:valid &= source!=src.nodata
        values=np.where(valid,source,0.0);weight=valid.astype('float64')
        shape=(contract['height'],contract['width']);num=np.zeros(shape,dtype='float64');den=np.zeros(shape,dtype='float64')
        reproject(values,num,src_transform=src.transform,src_crs=src.crs,dst_transform=transform_out,dst_crs=contract['crs'],resampling=resampling,src_nodata=None,dst_nodata=-9999)
        coverage_resampling=Resampling.average if method=='area' else resampling
        reproject(weight,den,src_transform=src.transform,src_crs=src.crs,dst_transform=transform_out,dst_crs=contract['crs'],resampling=coverage_resampling,src_nodata=None,dst_nodata=-9999)
        result=np.full(shape,-9999.0,dtype='float64');good=den>=contract['min_coverage'];good &= den>0
        result[good]=num[good] if method=='area' else num[good]/den[good]
        provenance=[]
        for r in range(shape[0]):
            for c in range(shape[1]):
                try:sr,sc=map_cell(src,transform_out,contract['crs'],r,c)
                except Exception:sr=sc=None
                provenance.append((r,c,float(result[r,c]) if good[r,c] else None,float(den[r,c]),sr,sc,float(den[r,c])))
        profile=src.profile.copy();profile.update(driver='GTiff',width=shape[1],height=shape[0],count=1,dtype='float64',crs=contract['crs'],transform=transform_out,nodata=-9999.0)
    out.parent.mkdir(parents=True,exist_ok=True);tmp=out.with_suffix('.tmp.tif')
    if out.exists():raise ValueError('output_exists')
    with rasterio.open(tmp,'w',**profile) as dst:dst.write(result,1)
    published=False
    try:
        with db:
            rid=db.execute("INSERT INTO run(source_id,contract_id,status,method) VALUES (?,?,?,?)",(src_id,cid,'采样中',method)).lastrowid
            db.executemany('INSERT INTO cell VALUES (?,?,?,?,?,?,?,?)',[(rid,*x) for x in provenance])
            for r,c,value,cov,sr,sc,weight in provenance:
                if value is None:db.execute('INSERT INTO issue(run_id,row,col,code,detail) VALUES (?,?,?,?,?)',(rid,r,c,'coverage_low',f'{cov:.3f}'))
            # The output is exposed only after the provenance rows are valid. An
            # ordinary write/publish failure rolls the ledger back and removes it.
            os.replace(tmp,out);published=True
            db.execute('INSERT INTO output VALUES (?,?,?)',(rid,str(out),sha(out.read_bytes())))
            db.execute("UPDATE run SET status=?,ended=CURRENT_TIMESTAMP WHERE id=?",('待核' if np.any(~good) else '已核验',rid))
    except Exception:
        tmp.unlink(missing_ok=True)
        if published:out.unlink(missing_ok=True)
        raise
    finally:db.close()
    audit(db_path,rid)
    return rid

def audit(db_path,rid):
    db=connect(db_path);row=db.execute('SELECT o.path,o.hash,c.body,s.path AS source_path,s.hash AS source_hash FROM output o JOIN run r ON r.id=o.run_id JOIN contract c ON c.id=r.contract_id JOIN source s ON s.id=r.source_id WHERE o.run_id=?',(rid,)).fetchone()
    if not row:raise ValueError('unknown_run')
    p=Path(row['path']);contract=json.loads(row['body']);issues=[]
    source_path=Path(row['source_path'])
    if not source_path.is_file() or sha(source_path.read_bytes())!=row['source_hash']:issues.append('source_hash')
    if not p.is_file() or sha(p.read_bytes())!=row['hash']:issues.append('output_hash')
    else:
        with rasterio.open(p) as src:
            if src.width!=contract['width'] or src.height!=contract['height'] or str(src.crs)!=str(rasterio.crs.CRS.from_string(contract['crs'])):issues.append('grid_mismatch')
            if not np.allclose(tuple(src.bounds),tuple(contract['bounds']),rtol=0,atol=1e-8):issues.append('bounds_mismatch')
            data=src.read(1);finite=data[data!=src.nodata]
            recorded=list(db.execute('SELECT row,col,value FROM cell WHERE run_id=?',(rid,)))
            if len(recorded)!=data.size:issues.append('cell_evidence_missing')
            elif any((r['value'] is None and data[r['row'],r['col']]!=src.nodata) or
                     (r['value'] is not None and not np.isclose(data[r['row'],r['col']],r['value'],rtol=1e-9,atol=1e-9))
                     for r in recorded):issues.append('cell_mismatch')
            if finite.size and not np.all(np.isfinite(finite)):issues.append('nonfinite')
            if contract['kind']=='extensive' and source_path.is_file() and 'source_hash' not in issues and not contract.get('survey_id'):
                with rasterio.open(source_path) as origin:
                    original=origin.read(1)
                    valid=original!=origin.nodata if origin.nodata is not None else np.isfinite(original)
                    expected=float(original[valid].sum());actual=float(finite.sum())
                    if abs(actual-expected)>max(1e-6,abs(expected)*float(contract.get('total_tolerance',0.01))):issues.append('total_mismatch')
    issues.extend(_audit_control_fit(db, rid, contract))
    with db:
        db.execute("DELETE FROM issue WHERE run_id=? AND code LIKE 'audit_%'",(rid,))
        for code in issues:
            db.execute('INSERT INTO issue(run_id,code,detail) VALUES (?,?,?)',
                       (rid,'audit_'+code,'independent output verification'))
        if issues:
            state='失败' if any(x in issues for x in ('source_hash','output_hash','grid_mismatch','bounds_mismatch','cell_mismatch','cell_evidence_missing','control_observation_tampered','control_fit_tampered','cell_basis_mismatch','control_evidence_missing')) else '待核'
            db.execute('UPDATE run SET status=? WHERE id=?',(state,rid))
    db.close();return issues


def _audit_control_fit(db, rid, contract):
    """Re-derive the correction from frozen observations and re-locate cells."""
    fit = db.execute('SELECT * FROM control_fit WHERE run_id=? AND status=?',
                     (rid, '采用')).fetchone()
    if not fit:
        return []
    issues = []
    survey = db.execute('SELECT * FROM control_survey WHERE id=?',
                        (fit['survey_id'],)).fetchone()
    if not survey:
        return ['control_evidence_missing']
    # 1. Frozen raw observations must still hash to the registered digest and
    #    must match the persisted point rows.
    raw = survey['raw']
    if sha(raw.encode('utf-8')) != survey['digest'] or fit['observation_digest'] != survey['digest']:
        issues.append('control_observation_tampered')
    body = json.loads(raw)
    db_points = {p['point_id']: dict(p) for p in db.execute(
        'SELECT * FROM control_point WHERE survey_id=?', (fit['survey_id'],))}
    raw_points = {p['id']: p for p in body['points']}
    if set(db_points) != set(raw_points):
        issues.append('control_observation_tampered')
    else:
        for pid, rp in raw_points.items():
            dp = db_points[pid]
            if any(abs(dp[k] - rp[k]) > 1e-12 for k in
                   ('source_row', 'source_col', 'target_x', 'target_y', 'weight')):
                issues.append('control_observation_tampered')
                break
    points = [dict(row) for row in db.execute(
        'SELECT point_id AS id,source_row,source_col,target_x,target_y,weight '
        'FROM control_point WHERE survey_id=? ORDER BY point_id', (fit['survey_id'],))]
    # 2. Re-fit and compare coefficients/nodes and per-point residuals.
    try:
        model, records = geometry.fit_model(fit['model'], points,
                                            float(fit['residual_limit']))
    except geometry.GeometryError:
        issues.append('control_fit_tampered')
        model = records = None
    if model is not None:
        stored_coef = json.loads(fit['coef'])
        if stored_coef != model.to_dict():
            issues.append('control_fit_tampered')
        stored_pts = {p['point_id']: dict(p) for p in db.execute(
            'SELECT * FROM control_fit_point WHERE fit_id=?', (fit['id'],))}
        if set(stored_pts) != {rec['point_id'] for rec in records}:
            issues.append('control_fit_tampered')
        else:
            for rec in records:
                sp = stored_pts[rec['point_id']]
                if (abs(sp['residual'] - rec['residual']) > 1e-9 or
                        abs(sp['predicted_x'] - rec['predicted_x']) > 1e-9 or
                        abs(sp['predicted_y'] - rec['predicted_y']) > 1e-9 or
                        bool(sp['accepted']) != rec['accepted']):
                    issues.append('control_fit_tampered')
                    break
    # 3. Re-locate every target cell center and compare the persisted basis.
    target_transform = from_bounds(*contract['bounds'],
                                   contract['width'], contract['height'])
    basis = { (b['row'], b['col']): dict(b) for b in db.execute(
        'SELECT * FROM cell_basis WHERE run_id=?', (rid,))}
    cell_rows = {(c['row'], c['col']): dict(c) for c in db.execute(
        'SELECT row,col,source_row,source_col FROM cell WHERE run_id=?', (rid,))}
    if model is not None and len(basis) == contract['width'] * contract['height']:
        cx, cy = _target_cell_centers(contract, target_transform, survey['target_crs'])
        for r in range(contract['height']):
            for c in range(contract['width']):
                b = basis[(r, c)]
                if abs(b['target_x'] - cx[r, c]) > 1e-9 or abs(b['target_y'] - cy[r, c]) > 1e-9:
                    issues.append('cell_basis_mismatch')
                    break
                try:
                    located = model.inverse(float(cx[r, c]), float(cy[r, c]))
                except geometry.GeometryError:
                    if b['status'] != '不可定位':
                        issues.append('cell_basis_mismatch')
                    continue
                srow, scol = located[0], located[1]
                if b['status'] == '不可定位' or b['source_row'] is None or b['source_col'] is None:
                    issues.append('cell_basis_mismatch')
                    break
                if abs(b['source_row'] - srow) > 1e-8 or abs(b['source_col'] - scol) > 1e-8:
                    issues.append('cell_basis_mismatch')
                    break
                cell = cell_rows.get((r, c))
                if cell is None or cell['source_row'] is None or cell['source_col'] is None:
                    issues.append('cell_basis_mismatch')
                    break
                if abs(cell['source_row'] - srow) > 1e-8 or abs(cell['source_col'] - scol) > 1e-8:
                    issues.append('cell_basis_mismatch')
                    break
            if 'cell_basis_mismatch' in issues:
                break
    else:
        issues.append('control_evidence_missing')
    return issues

def coverage(db_path,rid):
    db=connect(db_path);rows=[dict(x) for x in db.execute('SELECT row,col,coverage FROM cell WHERE run_id=? ORDER BY row,col',(rid,))];db.close();return rows

def report(db_path,rid,path):
    findings=audit(db_path,rid)
    db=connect(db_path);run_row=db.execute('SELECT * FROM run WHERE id=?',(rid,)).fetchone()
    if not run_row:raise ValueError('unknown_run')
    fit=db.execute('SELECT * FROM control_fit WHERE run_id=? ORDER BY id DESC',(rid,)).fetchall()
    control_block=None
    if fit:
        latest=dict(fit[-1])
        control_block={'fit':latest,
                       'points':[dict(x) for x in db.execute(
                           'SELECT * FROM control_fit_point WHERE fit_id=? ORDER BY point_id',
                           (latest['id'],))],
                       'basis':[dict(x) for x in db.execute(
                           'SELECT row,col,status,target_x,target_y,source_row,source_col,basis,reason FROM cell_basis WHERE run_id=? ORDER BY row,col',(rid,))]}
    obj={'run':dict(run_row),'output':dict(db.execute('SELECT * FROM output WHERE run_id=?',(rid,)).fetchone()),'issues':[dict(x) for x in db.execute('SELECT row,col,code,detail FROM issue WHERE run_id=?',(rid,))],'audit':findings,'control':control_block,'cells':[dict(x) for x in db.execute('SELECT row,col,value,coverage,source_row,source_col FROM cell WHERE run_id=? ORDER BY row,col',(rid,))]}
    path=Path(path);path.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
    correction=''
    if control_block:
        correction=f"，校正模型：{control_block['fit']['model']}（{control_block['fit']['status']}）"
    path.with_suffix('.md').write_text('# 栅格重投报告\n\n'+f"运行：{run_row['status']}，输出复核：{obj['audit']}{correction}\n",encoding='utf-8');db.close();return path
