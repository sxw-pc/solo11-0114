"""Reproject source rasters, propagate declared measurement error bounds and
audit the resulting nominal values plus per-cell uncertainty intervals."""

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds, rowcol
from rasterio.warp import transform, reproject, Resampling

from .store import connect


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'))


# ---------------------------------------------------------------------------
# Error model contract
# ---------------------------------------------------------------------------

def _validate_error_model(contract):
    """Validate and return the optional frozen error-model declaration.

    The model is either a uniform non-negative absolute bound for every valid
    source pixel, or a path to a same-grid local error raster. NoData in that
    raster means "unknown", never zero. Category variables have no numeric
    error model.
    """

    spec = contract.get('error_model')
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise ValueError('bad_error_model')
    if contract.get('kind') == 'category':
        raise ValueError('error_model_kind_conflict')
    if set(spec) - {'version', 'abs_bound', 'error_grid'}:
        raise ValueError('bad_error_model')
    version = spec.get('version')
    if not isinstance(version, str) or not version.strip():
        raise ValueError('bad_error_model')
    has_bound = 'abs_bound' in spec
    has_grid = 'error_grid' in spec
    if has_bound == has_grid:
        raise ValueError('bad_error_model')
    if has_bound:
        bound = spec['abs_bound']
        if (isinstance(bound, bool) or not isinstance(bound, (int, float)) or
                not np.isfinite(bound) or bound < 0):
            raise ValueError('bad_error_model')
    else:
        grid = spec['error_grid']
        if not isinstance(grid, str) or not grid.strip():
            raise ValueError('bad_error_model')
    return spec


def _model_hash(spec):
    """Content hash of the frozen model declaration.

    For a uniform model there is no separate file, so the canonical spec is
    hashed. For a local error grid the file bytes are hashed like the source.
    """

    if 'error_grid' in spec:
        path = Path(spec['error_grid'])
        if not path.is_file():
            raise ValueError('error_grid_unreadable')
        return sha(path.read_bytes())
    return sha(canon({'version': spec['version'], 'abs_bound': spec['abs_bound']}).encode())


def _load_error(spec, src, valid):
    """Return (error_bound_by_source_pixel, error_known_mask, relation)."""

    shape = (src.height, src.width)
    relation = None
    if 'abs_bound' in spec:
        return np.full(shape, float(spec['abs_bound']), dtype='float64'), valid.copy(), relation

    path = Path(spec['error_grid'])
    if not path.is_file():
        raise ValueError('error_grid_unreadable')
    with rasterio.open(path) as err:
        if (err.width != src.width or err.height != src.height or
                not np.allclose(tuple(err.transform)[:6], tuple(src.transform)[:6],
                                rtol=0, atol=1e-9) or
                rasterio.crs.CRS.from_user_input(err.crs) != src.crs):
            raise ValueError('error_grid_mismatch')
        relation = {
            'crs': str(err.crs),
            'width': err.width,
            'height': err.height,
            'transform': [float(x) for x in tuple(err.transform)[:6]],
            'nodata': None if err.nodata is None else float(err.nodata),
        }
        data = err.read(1).astype('float64')
        known = np.isfinite(data)
        if err.nodata is not None:
            known &= data != err.nodata
        if np.any(data[known] < 0):
            raise ValueError('error_grid_negative')
        bounds = np.where(known, data, 0.0)
    return bounds, valid & known, relation


def _resampling(method):
    return {'nearest': Resampling.nearest, 'bilinear': Resampling.bilinear,
            'area': Resampling.sum}[method]


def _reproject_grid(data, src, contract, transform_out, resampling):
    out = np.zeros((contract['height'], contract['width']), dtype='float64')
    reproject(data, out, src_transform=src.transform, src_crs=src.crs,
              dst_transform=transform_out, dst_crs=contract['crs'],
              resampling=resampling, src_nodata=None, dst_nodata=-9999)
    return out


def _compute_evidence(src, contract, values, valid, err, err_known, declared):
    """Reproject nominal values together with their uncertainty evidence.

    Bilinear intervals use the valid neighbours that actually take part in the
    normalisation (num/den), with the same normalisation applied to the
    weighted error bounds. Area intervals accumulate the error bound over the
    same area weights used for the amount. Any strictly necessary neighbour
    with unknown error makes the target interval unknown.
    """

    method = contract['method']
    transform_out = from_bounds(*contract['bounds'], contract['width'], contract['height'])
    shape = (contract['height'], contract['width'])
    sampling = _resampling(method)
    num = _reproject_grid(values, src, contract, transform_out, sampling)
    weight = valid.astype('float64')
    coverage_sampling = Resampling.average if method == 'area' else sampling
    den = _reproject_grid(weight, src, contract, transform_out, coverage_sampling)
    # Reference scale for deciding whether any unknown-error weight actually
    # contributes. Area weights accumulate (pixel areas), bilinear weights are
    # normalised fractions.
    if method == 'area':
        total_weight = _reproject_grid(weight, src, contract, transform_out, resampling=Resampling.sum)
    else:
        total_weight = den
    good = (den >= contract['min_coverage']) & (den > 0)
    nominal = np.full(shape, -9999.0, dtype='float64')
    nominal[good] = num[good] if method == 'area' else num[good] / den[good]

    lower = np.full(shape, np.nan)
    upper = np.full(shape, np.nan)
    bound = np.full(shape, np.nan)
    error_weight = np.zeros(shape)
    unknown_weight = np.zeros(shape)
    interval_known = np.zeros(shape, dtype=bool)
    if declared:
        err_values = np.where(valid & err_known, err, 0.0)
        unknown = (valid & ~err_known).astype('float64')
        try:
            error_weight = _reproject_grid(err_values, src, contract, transform_out, sampling)
            unknown_weight = _reproject_grid(unknown, src, contract, transform_out, sampling)
        except Exception:
            # A failed error transform leaves every target interval unknown
            # instead of silently treating the missing part as zero error.
            error_weight[:] = 0.0
            unknown_weight[:] = 1.0
        with np.errstate(divide='ignore', invalid='ignore'):
            bound = error_weight.copy() if method == 'area' else error_weight / den
        tolerance = 1e-9 * np.maximum(1.0, total_weight)
        interval_known = good & (unknown_weight <= tolerance) & np.isfinite(nominal) & np.isfinite(bound)
        lower = np.where(interval_known, nominal - bound, np.nan)
        upper = np.where(interval_known, nominal + bound, np.nan)
    return {
        'transform_out': transform_out, 'num': num, 'den': den, 'good': good,
        'nominal': nominal, 'lower': lower, 'upper': upper, 'bound': bound,
        'error_weight': error_weight, 'unknown_weight': unknown_weight,
        'total_weight': total_weight, 'interval_known': interval_known,
    }


def _weight_evidence(method, den, total_weight, error_weight, unknown_weight,
                     known, source_row, source_col):
    return canon({
        'mode': 'area' if method == 'area' else 'normalised_bilinear',
        'weight_sum': float(total_weight if method == 'area' else den),
        'coverage_weight': float(den),
        'error_weight_sum': float(error_weight),
        'unknown_weight_sum': float(unknown_weight),
        'bound': float(error_weight if method == 'area' else error_weight / den)
        if known and den > 0 else None,
        'source_row': None if source_row is None else float(source_row),
        'source_col': None if source_col is None else float(source_col),
    })


# ---------------------------------------------------------------------------
# Registration / plan
# ---------------------------------------------------------------------------

def inspect(db_path, path, unit='unit'):
    path = Path(path)
    raw = path.read_bytes()
    with rasterio.open(path) as src:
        if not src.crs or src.width <= 0 or src.height <= 0 or abs(src.transform.determinant) < 1e-15:
            raise ValueError('bad_source_grid')
        info = {'width': src.width, 'height': src.height, 'crs': str(src.crs),
                'nodata': src.nodata, 'unit': unit, 'hash': sha(raw)}
    db = connect(db_path)
    with db:
        db.execute('INSERT OR IGNORE INTO source(path,hash,width,height,crs,nodata,unit) '
                   'VALUES (?,?,?,?,?,?,?)',
                   (str(path), info['hash'], info['width'], info['height'],
                    info['crs'], info['nodata'], unit))
    db.close()
    return info


def plan(db_path, contract):
    needed = {'crs', 'bounds', 'width', 'height', 'kind', 'method', 'min_coverage'}
    if not needed.issubset(contract):
        raise ValueError('missing_contract')
    x0, y0, x1, y1 = contract['bounds']
    w = int(contract['width'])
    h = int(contract['height'])
    if w <= 0 or h <= 0 or x1 <= x0 or y1 <= y0 or not 0 <= contract['min_coverage'] <= 1:
        raise ValueError('bad_grid')
    methods = {'category': {'nearest'}, 'continuous': {'bilinear'}, 'extensive': {'area'}}
    if contract['method'] not in methods.get(contract['kind'], set()):
        raise ValueError('method_kind_conflict')
    _validate_error_model(contract)
    try:
        rasterio.crs.CRS.from_string(contract['crs'])
    except Exception as exc:
        raise ValueError('bad_crs') from exc
    body = canon(contract)
    db = connect(db_path)
    with db:
        db.execute('INSERT OR IGNORE INTO contract(hash,body) VALUES (?,?)',
                   (sha(body.encode()), body))
    db.close()
    return sha(body.encode())


def map_cell(src, target_transform, target_crs, r, c):
    x, y = target_transform @ (c + 0.5, r + 0.5)
    xs, ys = transform(target_crs, src.crs, [x], [y])
    return rowcol(src.transform, xs[0], ys[0], op=float)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run(db_path, source_path, contract, out):
    source_path = Path(source_path)
    out = Path(out)
    info = inspect(db_path, source_path)
    ch = plan(db_path, contract)
    spec = _validate_error_model(contract)
    model_body = canon(spec) if spec is not None else None
    model_hash = _model_hash(spec) if spec is not None else None

    db = connect(db_path)
    src_id = db.execute('SELECT id FROM source WHERE hash=?', (info['hash'],)).fetchone()['id']
    cid = db.execute('SELECT id FROM contract WHERE hash=?', (ch,)).fetchone()['id']
    transform_out = from_bounds(*contract['bounds'], contract['width'], contract['height'])
    method = contract['method']
    with rasterio.open(source_path) as src:
        source = src.read(1).astype('float64')
        valid = np.isfinite(source)
        if src.nodata is not None:
            valid &= source != src.nodata
        values = np.where(valid, source, 0.0)
        if spec is not None:
            err, err_known, relation = _load_error(spec, src, valid)
        else:
            err = err_known = relation = None
        ev = _compute_evidence(src, contract, values, valid, err, err_known, spec is not None)
        shape = (contract['height'], contract['width'])
        result = ev['nominal']
        good = ev['good']
        rows = []
        unknown_rows = []
        for r in range(shape[0]):
            for c in range(shape[1]):
                try:
                    sr, sc = map_cell(src, transform_out, contract['crs'], r, c)
                except Exception:
                    sr = sc = None
                # An inverse-map failure makes the cell's interval unknown:
                # nominal success alone must never be treated as safe.
                known = bool(ev['interval_known'][r, c]) and sr is not None and sc is not None
                value = float(result[r, c]) if good[r, c] else None
                lower = float(ev['lower'][r, c]) if known else None
                upper = float(ev['upper'][r, c]) if known else None
                if spec is not None and good[r, c] and not known:
                    if sr is None or sc is None:
                        unknown_rows.append((r, c, 'inverse_map'))
                    else:
                        unknown_rows.append((r, c, f"{float(ev['unknown_weight'][r, c]):.6g}"))
                evidence = _weight_evidence(method, float(ev['den'][r, c]),
                                            float(ev['total_weight'][r, c]),
                                            float(ev['error_weight'][r, c]),
                                            float(ev['unknown_weight'][r, c]),
                                            known, sr, sc)
                rows.append((r, c, value, float(ev['den'][r, c]),
                             None if sr is None else float(sr),
                             None if sc is None else float(sc),
                             float(ev['den'][r, c]), lower, upper,
                             None if spec is None else int(known), evidence))
        profile = src.profile.copy()
        profile.update(driver='GTiff', width=shape[1], height=shape[0], count=1,
                       dtype='float64', crs=contract['crs'], transform=transform_out,
                       nodata=-9999.0)

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix('.tmp.tif')
    if out.exists():
        raise ValueError('output_exists')
    rows_data = rows
    with rasterio.open(tmp, 'w', **profile) as dst:
        dst.write(result, 1)
    published = False
    try:
        with db:
            rid = db.execute(
                'INSERT INTO run(source_id,contract_id,status,method,error_model_version,'
                'error_model,error_hash,error_grid,error_grid_relation) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (src_id, cid, '采样中', method,
                 None if spec is None else spec['version'], model_body, model_hash,
                 None if spec is None or 'error_grid' not in spec else spec['error_grid'],
                 None if relation is None else canon(relation))).lastrowid
            db.executemany(
                'INSERT INTO cell(run_id,row,col,value,coverage,source_row,source_col,weight,'
                'value_lower,value_upper,error_known,weight_evidence) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                [(rid, *x) for x in rows_data])
            for r, c, value, cov, sr, sc, weight, lower, upper, known, evidence in rows_data:
                if value is None:
                    db.execute('INSERT INTO issue(run_id,row,col,code,detail) VALUES (?,?,?,?,?)',
                               (rid, r, c, 'coverage_low', f'{cov:.3f}'))
            for r, c, missing in unknown_rows:
                db.execute('INSERT INTO issue(run_id,row,col,code,detail) VALUES (?,?,?,?,?)',
                           (rid, r, c, 'error_unknown', missing))
            # The output is exposed only after the provenance rows are valid. An
            # ordinary write/publish failure rolls the ledger back and removes it.
            os.replace(tmp, out)
            published = True
            db.execute('INSERT INTO output VALUES (?,?,?)', (rid, str(out), sha(out.read_bytes())))
            db.execute("UPDATE run SET status=?,ended=CURRENT_TIMESTAMP WHERE id=?",
                       ('待核' if np.any(~good) else '已核验', rid))
    except Exception:
        tmp.unlink(missing_ok=True)
        if published:
            out.unlink(missing_ok=True)
        raise
    finally:
        db.close()
    audit(db_path, rid)
    return rid


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def _audit_error_model(db, row, contract, issues):
    """Check the frozen second source, its grid relation and interval evidence.

    Returns the recomputation context, or None when it cannot be trusted.
    """

    run = db.execute('SELECT * FROM run WHERE id=?', (row['run_id'],)).fetchone()
    stored = run['error_model']
    declared = contract.get('error_model')
    if stored is None and declared is None:
        return None
    if stored is None or declared is None:
        issues.append('error_model')
        return None
    try:
        spec = json.loads(stored)
    except Exception:
        issues.append('error_model')
        return None
    if canon(spec) != canon(declared) or run['error_model_version'] != spec.get('version'):
        issues.append('error_model')
        return None
    zone_decision = db.execute(
        'SELECT model_version,model_hash FROM zone_decision WHERE run_id=?', (row['run_id'],)
    ).fetchall()
    if any((zd['model_version'] not in (None, run['error_model_version'])) or
           (zd['model_hash'] not in (None, run['error_hash'])) for zd in zone_decision):
        issues.append('zone_model_mismatch')
        return None
    release_policy = db.execute(
        'SELECT policy FROM release_request WHERE run_id=?', (row['run_id'],)).fetchall()
    for release in release_policy:
        if release['policy'] is None:
            continue
        try:
            frozen = json.loads(release['policy'])
        except Exception:
            issues.append('release_policy_mismatch')
            return None
        if frozen.get('model_version') != run['error_model_version'] or \
                any(z.get('run_model_hash') != run['error_hash']
                    for z in frozen.get('zones', [])):
            issues.append('release_policy_mismatch')
            return None
    source_path = Path(row['source_path'])
    if not source_path.is_file():
        issues.append('source_hash')
        return None
    if 'error_grid' in spec:
        grid = Path(spec['error_grid'])
        if not grid.is_file():
            issues.append('error_grid')
            return None
        if sha(grid.read_bytes()) != run['error_hash']:
            issues.append('error_hash')
            return None
        try:
            with rasterio.open(grid) as es, rasterio.open(source_path) as ss:
                relation = {
                    'crs': str(es.crs), 'width': es.width, 'height': es.height,
                    'transform': [float(x) for x in tuple(es.transform)[:6]],
                    'nodata': None if es.nodata is None else float(es.nodata),
                }
                if canon(relation) != run['error_grid_relation']:
                    issues.append('error_grid_mismatch')
                    return None
                if (es.width != ss.width or es.height != ss.height or
                        not np.allclose(tuple(es.transform)[:6], tuple(ss.transform)[:6],
                                        rtol=0, atol=1e-9) or
                        rasterio.crs.CRS.from_user_input(es.crs) !=
                        rasterio.crs.CRS.from_user_input(ss.crs)):
                    issues.append('error_grid_mismatch')
                    return None
        except Exception:
            issues.append('error_grid')
            return None
    else:
        if _model_hash(spec) != run['error_hash']:
            issues.append('error_model')
            return None
    try:
        with rasterio.open(source_path) as src:
            source = src.read(1).astype('float64')
            valid = np.isfinite(source)
            if src.nodata is not None:
                valid &= source != src.nodata
            values = np.where(valid, source, 0.0)
            err, err_known, _ = _load_error(spec, src, valid)
            ev = _compute_evidence(src, contract, values, valid, err, err_known, True)
    except Exception:
        issues.append('error_grid')
        return None
    return ev, spec


def _same_number(a, b):
    if a is None or b is None:
        return a is None and b is None
    return np.isclose(float(a), float(b), rtol=1e-9, atol=1e-12)


def _audit_zone_evidence(db, rid, issues):
    """Re-check persisted zone intervals and release envelopes against cells.

    Zone decisions and release requests must stay consistent with the frozen
    per-cell evidence; a rewritten bound is not allowed to keep its old verdict.
    """

    decisions = db.execute(
        'SELECT zd.*, z.strategy AS zone_strategy, z.cell_ids, z.limit_value '
        'FROM zone_decision zd JOIN zone z ON z.id=zd.zone_id WHERE zd.run_id=?',
        (rid,)).fetchall()
    bounds = {}
    for decision in decisions:
        strategy = decision['zone_strategy']
        if decision['strategy'] != strategy:
            issues.append('zone_evidence_mismatch')
            return
        if strategy is None:
            continue
        try:
            ids = json.loads(decision['cell_ids'])
            stored_evidence = json.loads(decision['evidence'])
        except Exception:
            issues.append('zone_evidence_mismatch')
            return
        lows = []
        highs = []
        all_known = True
        if not isinstance(stored_evidence, list) or len(stored_evidence) != len(ids):
            issues.append('zone_evidence_mismatch')
            return
        by_key = {(e.get('row'), e.get('col')): e for e in stored_evidence
                  if isinstance(e, dict)}
        for r, c in ids:
            cell = db.execute(
                'SELECT value,value_lower,value_upper,error_known FROM cell '
                'WHERE run_id=? AND row=? AND col=?', (rid, r, c)).fetchone()
            if cell is None:
                issues.append('zone_evidence_mismatch')
                return
            entry = by_key.get((r, c))
            if entry is None:
                issues.append('zone_evidence_mismatch')
                return
            known = (cell['error_known'] == 1 and cell['value'] is not None and
                     cell['value_lower'] is not None and cell['value_upper'] is not None)
            if (entry.get('error_known') != (1 if known else 0) or
                    not _same_number(entry.get('lower'), cell['value_lower']) or
                    not _same_number(entry.get('upper'), cell['value_upper'])):
                issues.append('zone_evidence_mismatch')
                return
            if known:
                lows.append(float(cell['value_lower']))
                highs.append(float(cell['value_upper']))
            else:
                all_known = False
        if all_known and ids:
            expected_low = sum(lows) / len(lows) if strategy == 'mean_bound' else max(lows)
            expected_high = sum(highs) / len(highs) if strategy == 'mean_bound' else max(highs)
        else:
            expected_low = expected_high = None
        if not _same_number(decision['lower_bound'], expected_low) or \
                not _same_number(decision['upper_bound'], expected_high):
            issues.append('zone_evidence_mismatch')
            return
        if expected_low is None or expected_high is None:
            bounds[decision['zone_id']] = (None, None)
        else:
            bounds[decision['zone_id']] = (expected_low, expected_high)

    for release in db.execute(
            'SELECT * FROM release_request WHERE run_id=? AND policy IS NOT NULL', (rid,)):
        try:
            policy = json.loads(release['policy'])
            entries = policy.get('zones', [])
            names = [z['zone'] for z in entries]
        except Exception:
            issues.append('zone_evidence_mismatch')
            return
        # The frozen policy must still describe the registered zone contracts.
        for entry in entries:
            zone = db.execute('SELECT * FROM zone WHERE id=?', (entry['zone'],)).fetchone()
            if zone is None or entry.get('strategy') != zone['strategy'] or \
                    entry.get('strategy_version') != zone['strategy_version'] or \
                    entry.get('limit_value') != zone['limit_value'] or \
                    entry.get('min_coverage') != zone['min_coverage'] or \
                    entry.get('cells') != json.loads(zone['cell_ids']):
                issues.append('zone_evidence_mismatch')
                return
        values = [bounds.get(name, (None, None)) for name in names]
        if values and all(v[0] is not None for v in values):
            expected_low = min(v[0] for v in values)
            expected_high = max(v[1] for v in values)
        else:
            expected_low = expected_high = None
        if not _same_number(release['lower_bound'], expected_low) or \
                not _same_number(release['upper_bound'], expected_high):
            issues.append('zone_evidence_mismatch')
            return


def audit(db_path, rid):
    db = connect(db_path)
    row = db.execute(
        'SELECT o.path,o.hash,c.body,s.path AS source_path,s.hash AS source_hash '
        'FROM output o JOIN run r ON r.id=o.run_id JOIN contract c ON c.id=r.contract_id '
        'JOIN source s ON s.id=r.source_id WHERE o.run_id=?', (rid,)).fetchone()
    if not row:
        db.close()
        raise ValueError('unknown_run')
    p = Path(row['path'])
    contract = json.loads(row['body'])
    issues = []
    source_path = Path(row['source_path'])
    if not source_path.is_file() or sha(source_path.read_bytes()) != row['source_hash']:
        issues.append('source_hash')
    if not p.is_file() or sha(p.read_bytes()) != row['hash']:
        issues.append('output_hash')
    else:
        with rasterio.open(p) as src:
            if (src.width != contract['width'] or src.height != contract['height'] or
                    str(src.crs) != str(rasterio.crs.CRS.from_string(contract['crs']))):
                issues.append('grid_mismatch')
            if not np.allclose(tuple(src.bounds), tuple(contract['bounds']),
                               rtol=0, atol=1e-8):
                issues.append('bounds_mismatch')
            data = src.read(1)
            finite = data[data != src.nodata]
            recorded = list(db.execute(
                'SELECT row,col,value,value_lower,value_upper,error_known,weight_evidence '
                'FROM cell WHERE run_id=?', (rid,)))
            if len(recorded) != data.size:
                issues.append('cell_evidence_missing')
            elif any((r['value'] is None and data[r['row'], r['col']] != src.nodata) or
                     (r['value'] is not None and
                      not np.isclose(data[r['row'], r['col']], r['value'],
                                     rtol=1e-9, atol=1e-9))
                     for r in recorded):
                issues.append('cell_mismatch')
            if finite.size and not np.all(np.isfinite(finite)):
                issues.append('nonfinite')
            if contract['kind'] == 'extensive' and source_path.is_file() and \
                    'source_hash' not in issues:
                with rasterio.open(source_path) as origin:
                    original = origin.read(1)
                    valid = original != origin.nodata if origin.nodata is not None \
                        else np.isfinite(original)
                    expected = float(original[valid].sum())
                    actual = float(finite.sum())
                    if abs(actual - expected) > max(
                            1e-6, abs(expected) * float(contract.get('total_tolerance', 0.01))):
                        issues.append('total_mismatch')

    model_ctx = None
    if not issues or issues == ['total_mismatch']:
        model_ctx = _audit_error_model(db, {**dict(row), 'run_id': rid}, contract, issues)
    if model_ctx is not None and 'cell_evidence_missing' not in issues:
        ev, spec = model_ctx
        source_path = Path(row['source_path'])
        with rasterio.open(source_path) as src:
            cells = list(db.execute(
                'SELECT row,col,value,coverage,value_lower,value_upper,error_known,'
                'weight_evidence FROM cell WHERE run_id=?', (rid,)))
            transform_out = from_bounds(*contract['bounds'], contract['width'],
                                        contract['height'])
            mismatch = False
            for cell in cells:
                r, c = cell['row'], cell['col']
                try:
                    sr, sc = map_cell(src, transform_out, contract['crs'], r, c)
                except Exception:
                    sr = sc = None
                known = bool(ev['interval_known'][r, c]) and sr is not None and sc is not None
                expected_known = 1 if known else 0
                if cell['error_known'] != expected_known:
                    mismatch = True
                    continue
                den = float(ev['den'][r, c])
                bound = (float(ev['error_weight'][r, c]) if contract['method'] == 'area'
                         else (float(ev['error_weight'][r, c] / den) if den > 0 else None))
                if known:
                    value_weight = (float(ev['total_weight'][r, c])
                                    if contract['method'] == 'area' else den)
                    if (cell['value_lower'] is None or cell['value_upper'] is None or
                            not np.isclose(cell['value_lower'],
                                           float(ev['lower'][r, c]), rtol=1e-7, atol=1e-9) or
                            not np.isclose(cell['value_upper'],
                                           float(ev['upper'][r, c]), rtol=1e-7, atol=1e-9) or
                            not _same_number(cell['value'], ev['nominal'][r, c]) or
                            not (cell['value_lower'] <= cell['value'] <= cell['value_upper'])):
                        mismatch = True
                        continue
                else:
                    value_weight = den
                    if cell['value_lower'] is not None or cell['value_upper'] is not None:
                        mismatch = True
                        continue
                try:
                    evidence = json.loads(cell['weight_evidence'])
                except Exception:
                    evidence = None
                if not isinstance(evidence, dict) or \
                        not np.isclose(float(evidence.get('weight_sum', np.nan)), value_weight,
                                       rtol=1e-9, atol=1e-9) or \
                        not np.isclose(float(evidence.get('error_weight_sum', np.nan)),
                                       float(ev['error_weight'][r, c]),
                                       rtol=1e-9, atol=1e-9) or \
                        not np.isclose(float(evidence.get('unknown_weight_sum', np.nan)),
                                       float(ev['unknown_weight'][r, c]),
                                       rtol=1e-9, atol=1e-9):
                    mismatch = True
                    continue
                if known and evidence.get('bound') is not None:
                    if not np.isclose(float(evidence['bound']), bound,
                                      rtol=1e-7, atol=1e-9):
                        mismatch = True
            if mismatch:
                issues.append('interval_mismatch')

    if not issues:
        _audit_zone_evidence(db, rid, issues)

    with db:
        db.execute("DELETE FROM issue WHERE run_id=? AND code LIKE 'audit_%'", (rid,))
        for code in issues:
            db.execute('INSERT INTO issue(run_id,code,detail) VALUES (?,?,?)',
                       (rid, 'audit_' + code, 'independent output verification'))
        if issues:
            fatal = {'source_hash', 'output_hash', 'grid_mismatch', 'bounds_mismatch',
                     'cell_mismatch', 'cell_evidence_missing', 'error_hash', 'error_grid',
                     'error_grid_mismatch', 'error_model', 'interval_mismatch',
                     'zone_model_mismatch', 'release_policy_mismatch',
                     'zone_evidence_mismatch'}
            state = '失败' if any(x in fatal for x in issues) else '待核'
            db.execute('UPDATE run SET status=? WHERE id=?', (state, rid))
            # A previously granted release cannot survive a failed independent
            # check; invalidate it rather than leaving a stale green verdict.
            db.execute("UPDATE release_request SET status='暂缓',reason='audit_failed' "
                       "WHERE run_id=? AND status='准予发布'", (rid,))
    db.close()
    return issues


# ---------------------------------------------------------------------------
# Coverage / report
# ---------------------------------------------------------------------------

def coverage(db_path, rid):
    db = connect(db_path)
    rows = [dict(x) for x in db.execute(
        'SELECT row,col,coverage FROM cell WHERE run_id=? ORDER BY row,col', (rid,))]
    db.close()
    return rows


def report(db_path, rid, path):
    findings = audit(db_path, rid)
    db = connect(db_path)
    run_row = db.execute('SELECT * FROM run WHERE id=?', (rid,)).fetchone()
    if not run_row:
        db.close()
        raise ValueError('unknown_run')
    zone_rows = [dict(x) for x in db.execute(
        'SELECT zd.*, z.limit_value FROM zone_decision zd '
        'JOIN zone z ON z.id=zd.zone_id WHERE zd.run_id=? ORDER BY zd.zone_id', (rid,))]
    release_rows = [dict(x) for x in db.execute(
        'SELECT * FROM release_request WHERE run_id=? ORDER BY id', (rid,))]
    cells = []
    for x in db.execute(
            'SELECT row,col,value,coverage,source_row,source_col,value_lower,value_upper,'
            'error_known FROM cell WHERE run_id=? ORDER BY row,col', (rid,)):
        cells.append(dict(x))
    zones = []
    interval_run = run_row['error_model'] is not None
    for z in zone_rows:
        try:
            evidence = json.loads(z.get('evidence') or '[]')
        except Exception:
            evidence = []
        limit = z.get('limit_value')
        flagged = []
        interval_zone = interval_run and z.get('strategy') is not None
        if interval_zone and isinstance(evidence, list):
            for cell in evidence:
                if not isinstance(cell, dict):
                    continue
                lower = cell.get('lower')
                upper = cell.get('upper')
                crosses = (lower is not None and upper is not None and
                           lower <= limit < upper)
                missing = cell.get('error_known') not in (1,)
                if crosses or missing:
                    flagged.append({**cell, 'crosses_limit': crosses,
                                    'missing_error': missing})
        z = dict(z)
        z['flagged_cells'] = flagged
        zones.append(z)
    obj = {'run': dict(run_row),
           'output': dict(db.execute('SELECT * FROM output WHERE run_id=?', (rid,)).fetchone()),
           'issues': [dict(x) for x in db.execute(
               'SELECT row,col,code,detail FROM issue WHERE run_id=?', (rid,))],
           'audit': findings, 'cells': cells, 'zones': zones, 'releases': release_rows}
    path = Path(path)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 栅格重投报告', '',
             f"运行：{run_row['status']}，输出复核：{obj['audit']}", '',
             '## 分区证据', '']
    if not zones:
        lines.append('（本运行尚无分区裁定）')
    for z in zones:
        lines.append(f"- 分区 {z['zone_id']}：{z['status']}，策略 {z.get('strategy') or '旧版均值'}，"
                     f"下界 {z.get('lower_bound')}，上界 {z.get('upper_bound')}，阈值 {z.get('limit_value')}")
        for cell in z['flagged_cells']:
            lines.append(f"  - 格 ({cell.get('row')},{cell.get('col')})：区间 "
                         f"[{cell.get('lower')}, {cell.get('upper')}]"
                         f"{'，跨阈值' if cell.get('crosses_limit') else ''}"
                         f"{'，误差未知' if cell.get('missing_error') else ''}")
    lines += ['', '## 发布请求', '']
    if not release_rows:
        lines.append('（本运行尚无发布请求）')
    for rel in release_rows:
        lines.append(f"- {rel['id']}：{rel['status']}（{rel['reason']}）")
    path.with_suffix('.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    db.close()
    return path
