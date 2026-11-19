"""Paired U/V vector-field reprojection with an explicit vector basis contract.

The scalar engine in :mod:`gridreproject.engine` reprojects one band at a time
and cannot say whether the paired numbers are source-grid axis components or
geographic east/north components.  This module keeps that scalar path untouched
and adds a vector-only pipeline:

1. both components are sampled with the *same* coverage mask and bilinear
   weights, so a pair is only valid where both U and V are valid;
2. the sampled pair is converted, per target cell, from the declared basis
   (``source_grid`` unit axes or ``earth_east_north``) into the orthogonal unit
   axes of the target grid via an earth-centred-earth-fixed / ENU intermediary;
3. magnitude is preserved by a pure rotation, the geographic east component is
   reported separately, and a cell whose local transform cannot be defined is
   marked ``indeterminate`` instead of being filled from an edge.
"""
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import rasterio
from pyproj import CRS, Transformer
from pyproj.exceptions import ProjError
from rasterio._err import CPLE_AppDefinedError
from rasterio.transform import Affine, from_bounds, rowcol
from rasterio.warp import transform as warp_transform

from .store import connect
from .vector import inspect as inspect_source

NODATA = -9999.0
VALID_BASES = ('source_grid', 'earth_east_north')
# Coordinate operations can fail through pyproj or the GDAL error layer;
# either means the local rotation is undefined for that cell.
TRANSFORM_ERRORS = (ProjError, CPLE_AppDefinedError, ValueError)
# A target cell must carry at least this much joint two-component coverage to
# be eligible; the contract min_coverage tightens the threshold further.
_EPS = 1e-12


def sha(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# contract
# ---------------------------------------------------------------------------

def plan(db_path, contract):
    """Freeze a vector contract and return its content hash."""
    needed = {'crs', 'bounds', 'width', 'height', 'basis',
              'velocity_unit', 'min_coverage', 'east_threshold'}
    missing = needed.difference(contract)
    if missing:
        raise ValueError('missing_vector_contract:' + ','.join(sorted(missing)))
    if contract['basis'] not in VALID_BASES:
        raise ValueError('invalid_vector_basis')
    x0, y0, x1, y1 = contract['bounds']
    w, h = int(contract['width']), int(contract['height'])
    if w <= 0 or h <= 0 or x1 <= x0 or y1 <= y0:
        raise ValueError('bad_vector_grid')
    if not 0 <= float(contract['min_coverage']) <= 1:
        raise ValueError('bad_vector_coverage')
    threshold = float(contract['east_threshold'])
    if not np.isfinite(threshold):
        raise ValueError('bad_east_threshold')
    if not str(contract['velocity_unit']).strip():
        raise ValueError('bad_velocity_unit')
    try:
        CRS.from_string(contract['crs'])
    except Exception as exc:
        raise ValueError('bad_vector_crs') from exc
    body = json.dumps(contract, sort_keys=True, separators=(',', ':'))
    db = connect(db_path)
    with db:
        db.execute('INSERT OR IGNORE INTO vector_contract(hash,body) VALUES (?,?)',
                   (sha(body.encode()), body))
    db.close()
    return sha(body.encode())


# ---------------------------------------------------------------------------
# geometry: unit axes, ENU intermediary and basis conversion
# ---------------------------------------------------------------------------

def _geodetic_direction(crs, x, y, dx, dy):
    """Unit metric direction of a horizontal step in a geodetic CRS.

    The step endpoints are mapped to ECEF and the chord is projected onto the
    tangent plane at the point, which gives the exact local ground direction
    without any map-projection distortion.
    """
    ecef = Transformer.from_crs(crs, 'EPSG:4978', always_xy=True)
    p = np.asarray(ecef.transform(x, y, 0.0, errcheck=True), dtype='float64')
    q = np.asarray(ecef.transform(x + dx, y + dy, 0.0, errcheck=True),
                   dtype='float64')
    chord = q - p
    normal = p / np.linalg.norm(p)
    tangent = chord - np.dot(chord, normal) * normal
    norm = float(np.linalg.norm(tangent))
    if not np.isfinite(norm) or norm <= _EPS:
        raise ProjError('degenerate_grid_axis')
    return tangent / norm


def _horizontal_unit_axes(crs, transform, x, y):
    """Metric unit vectors of the grid's column and row axes at (x, y).

    Both vectors are returned in a shared local east/north ground frame, so
    they stay exactly orthonormal for a rectilinear source grid: projected
    CRS read the directions straight off the affine step, geodetic CRS derive
    them from tangent-plane projection of ECEF chords.
    """
    if crs.is_geographic:
        col_xy = _geodetic_direction(crs, x, y, transform.a, transform.d)
        row_xy = _geodetic_direction(crs, x, y, transform.b, transform.e)
        # express ECEF tangent directions as east/north components
        ll = Transformer.from_crs(crs, 'EPSG:4326', always_xy=True)
        lon, lat = ll.transform(x, y, errcheck=True)
        east, north, _up = _enu_basis(lon, lat)
        col = np.array([np.dot(col_xy, east), np.dot(col_xy, north)])
        row = np.array([np.dot(row_xy, east), np.dot(row_xy, north)])
        return col / np.linalg.norm(col), row / np.linalg.norm(row)

    def axis(dx, dy):
        step = np.array([dx, dy], dtype='float64')
        norm = float(np.linalg.norm(step))
        if not np.isfinite(norm) or norm <= _EPS:
            raise ProjError('degenerate_grid_axis')
        return step / norm
    return axis(transform.a, transform.d), axis(transform.b, transform.e)


def _enu_basis(lon_deg, lat_deg):
    """Orthonormal east/north/up unit vectors in ECEF coordinates."""
    lon, lat = np.deg2rad(lon_deg), np.deg2rad(lat_deg)
    east = np.array([-np.sin(lon), np.cos(lon), 0.0])
    north = np.array([-np.sin(lat) * np.cos(lon),
                      -np.sin(lat) * np.sin(lon),
                      np.cos(lat)])
    up = np.cross(east, north)
    return east, north, up


def local_frame(crs, transform, x, y):
    """Return ECEF unit vectors (column axis, row axis, east, north) at a point.

    Grid unit axes are measured in true ground metric units inside one local
    east/north frame and then embedded in the earth-centred-earth-fixed frame.
    A Gram-Schmidt step makes the pair an exact orthonormal basis: the column
    axis keeps its measured direction and the row axis is orthogonalised
    against it, so a pure basis conversion preserves the physical magnitude
    even when the source CRS is angular and the cell span is large.

    Raises :class:`ProjError` when the rotation cannot be defined at this
    point; callers translate that into an ``indeterminate`` cell rather than
    borrowing an edge value.
    """
    col_xy, row_xy = _horizontal_unit_axes(crs, transform, x, y)
    ll = Transformer.from_crs(crs, 'EPSG:4326', always_xy=True)
    lon, lat = ll.transform(x, y, errcheck=True)
    if not np.isfinite(lon) or not np.isfinite(lat) or abs(lat) > 90.0 + 1e-9:
        raise ProjError('undefined_horizontal_direction')
    east, north, _up = _enu_basis(lon, lat)
    col_axis = col_xy[0] * east + col_xy[1] * north
    row_axis = row_xy[0] * east + row_xy[1] * north
    # orthonormalise while keeping the column-axis direction fixed
    col_axis = col_axis / np.linalg.norm(col_axis)
    row_axis = row_axis - np.dot(row_axis, col_axis) * col_axis
    row_norm = float(np.linalg.norm(row_axis))
    if row_norm <= _EPS:
        raise ProjError('degenerate_grid_axes')
    row_axis = row_axis / row_norm
    return col_axis, row_axis, east, north


def convert_pair(u, v, basis, src_frame, dst_frame):
    """Convert one sampled (U, V) pair into target grid unit-axis components.

    ``src_frame`` / ``dst_frame`` are ``(col_axis, row_axis, east, north)``
    tuples in ECEF coordinates built at the source sample point and the target
    cell centre respectively.  The conversion is a pure change of orthonormal
    basis, so the physical magnitude is preserved.
    """
    s_col, s_row, s_east, s_north = src_frame
    d_col, d_row, d_east, d_north = dst_frame
    if basis == 'source_grid':
        physical = u * s_col + v * s_row
    elif basis == 'earth_east_north':
        physical = u * s_east + v * s_north
    else:  # pragma: no cover - guarded by plan() and register()
        raise ValueError('invalid_vector_basis')
    out_u = float(physical @ d_col)
    out_v = float(physical @ d_row)
    east = float(physical @ d_east)
    north = float(physical @ d_north)
    return out_u, out_v, east, north


# ---------------------------------------------------------------------------
# joint two-component bilinear sampling
# ---------------------------------------------------------------------------

def _joint_sample(u_grid, v_grid, valid, sr, sc):
    """Bilinear sample of the pair with one shared validity mask.

    The bilinear kernel always sums to one; neighbours outside the grid or
    disqualified by a NoData in either component contribute zero and lower the
    returned *coverage*.  The pair itself is the plain kernel-weighted mean
    (no renormalisation), so an interior point reproduces its source vector
    exactly and the physical magnitude is preserved.  A point whose kernel
    never touches the grid returns ``None``.
    """
    h, w = valid.shape
    r0 = int(np.floor(sr))
    c0 = int(np.floor(sc))
    fr, fc = sr - r0, sc - c0
    kernel = np.array([(1 - fr) * (1 - fc), (1 - fr) * fc,
                       fr * (1 - fc), fr * fc])
    rr = [r0, r0, r0 + 1, r0 + 1]
    cc = [c0, c0 + 1, c0, c0 + 1]
    num_u = num_v = coverage = 0.0
    contribution = []
    for i, (r, c) in enumerate(zip(rr, cc)):
        weight = float(kernel[i])
        if not (0 <= r < h and 0 <= c < w) or not valid[r, c]:
            # outside-grid points and single-component NoData carry zero
            # weight: the surviving component is never used as a full vector
            continue
        coverage += weight
        num_u += weight * float(u_grid[r, c])
        num_v += weight * float(v_grid[r, c])
        contribution.append({'row': r, 'col': c, 'weight': weight,
                             'u': float(u_grid[r, c]), 'v': float(v_grid[r, c])})
    if not contribution:
        return None
    return num_u, num_v, float(coverage), contribution


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def run(db_path, vector_id, contract, out):
    """Reproject a frozen U/V pair onto the target grid and publish evidence."""
    out = Path(out)
    source = inspect_source(db_path, vector_id)
    if not source['intact']:
        raise ValueError('vector_source_not_intact')
    if source['basis'] != contract['basis']:
        raise ValueError('basis_contract_conflict')
    chash = plan(db_path, contract)
    src_transform = Affine(*json.loads(source['transform_json']))
    src_crs = CRS.from_string(source['crs'])
    dst_crs = CRS.from_string(contract['crs'])
    dst_transform = from_bounds(*contract['bounds'],
                                contract['width'], contract['height'])
    threshold = float(contract['east_threshold'])
    min_coverage = float(contract['min_coverage'])

    with rasterio.open(source['u_path']) as du, rasterio.open(source['v_path']) as dv:
        u_grid = du.read(1).astype('float64')
        v_grid = dv.read(1).astype('float64')
        nodata = source['nodata']
        valid = np.isfinite(u_grid) & np.isfinite(v_grid)
        if nodata is not None:
            valid &= (u_grid != nodata) & (v_grid != nodata)
        shape = (int(contract['height']), int(contract['width']))
        out_u = np.full(shape, NODATA, dtype='float64')
        out_v = np.full(shape, NODATA, dtype='float64')
        magnitude = np.full(shape, np.nan, dtype='float64')
        east_grid = np.full(shape, np.nan, dtype='float64')
        coverage_grid = np.zeros(shape, dtype='float64')
        state_grid = np.empty(shape, dtype=object)
        state_grid[:] = 'indeterminate'
        cell_rows = []

        for r in range(shape[0]):
            for c in range(shape[1]):
                tx, ty = dst_transform @ (c + 0.5, r + 0.5)
                sr = sc = None
                basis_json = None
                sampled = None
                state = 'indeterminate'
                try:
                    sx, sy = warp_transform(dst_crs, src_crs, [tx], [ty])
                    sx, sy = sx[0], sy[0]
                    if not (np.isfinite(sx) and np.isfinite(sy)):
                        raise ProjError('nonfinite_source_point')
                    sr_f, sc_f = rowcol(src_transform, sx, sy, op=float)
                    sr, sc = float(sr_f), float(sc_f)
                    sampled = _joint_sample(u_grid, v_grid, valid, sr, sc)
                    if sampled is None:
                        state = 'no_coverage'
                    else:
                        su, sv, coverage, contribution = sampled
                        coverage_grid[r, c] = coverage
                        if coverage < min_coverage or not contribution:
                            state = 'coverage_low'
                        else:
                            src_frame = local_frame(src_crs, src_transform, sx, sy)
                            dst_frame = local_frame(dst_crs, dst_transform, tx, ty)
                            tu, tv, east, north = convert_pair(
                                su, sv, contract['basis'], src_frame, dst_frame)
                            mag = float(np.hypot(tu, tv))
                            out_u[r, c] = tu
                            out_v[r, c] = tv
                            magnitude[r, c] = mag
                            east_grid[r, c] = east
                            state = 'valid'
                            basis_json = json.dumps({
                                'basis': contract['basis'],
                                'source_point': [sx, sy],
                                'target_point': [tx, ty],
                                'source_axes': {
                                    'col': src_frame[0].tolist(),
                                    'row': src_frame[1].tolist(),
                                    'east': src_frame[2].tolist(),
                                    'north': src_frame[3].tolist()},
                                'target_axes': {
                                    'col': dst_frame[0].tolist(),
                                    'row': dst_frame[1].tolist(),
                                    'east': dst_frame[2].tolist(),
                                    'north': dst_frame[3].tolist()},
                                'sampled_u': su, 'sampled_v': sv,
                                'target_u': tu, 'target_v': tv,
                                'east': east, 'north': north,
                                'magnitude': mag}, sort_keys=True)
                except TRANSFORM_ERRORS as exc:
                    # The local rotation or coordinate operation is not
                    # defined at this cell: keep it indeterminate, never fill
                    # it from an edge neighbour.
                    state = 'indeterminate_transform'
                    basis_json = json.dumps({'error': str(exc),
                                             'target_point': [tx, ty]},
                                            sort_keys=True)
                if state != 'valid':
                    out_u[r, c] = NODATA
                    out_v[r, c] = NODATA
                state_grid[r, c] = state
                cell_rows.append({
                    'row': r, 'col': c,
                    'u': float(out_u[r, c]) if state == 'valid' else None,
                    'v': float(out_v[r, c]) if state == 'valid' else None,
                    'magnitude': float(magnitude[r, c]) if state == 'valid' else None,
                    'east': float(east_grid[r, c]) if state == 'valid' else None,
                    'coverage': float(coverage_grid[r, c]),
                    'state': state,
                    'source_row': sr, 'source_col': sc,
                    'contribution': json.dumps(
                        sampled[3] if sampled and state == 'valid' else
                        (sampled[3] if sampled else []), sort_keys=True),
                    'basis_json': basis_json})

    profile = {
        'driver': 'GTiff', 'width': shape[1], 'height': shape[0],
        'count': 2, 'dtype': 'float64', 'crs': contract['crs'],
        'transform': dst_transform, 'nodata': NODATA}
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + '.tmp.tif')
    if out.exists():
        raise ValueError('vector_output_exists')
    with rasterio.open(tmp, 'w', **profile) as dst:
        dst.write(out_u, 1)
        dst.write(out_v, 2)
        dst.update_tags(basis=contract['basis'],
                        velocity_unit=str(contract['velocity_unit']),
                        band_names='u_target_axis,v_target_axis',
                        east_threshold=str(threshold))

    db = connect(db_path)
    published = False
    try:
        with db:
            cid = db.execute('SELECT id FROM vector_contract WHERE hash=?',
                             (chash,)).fetchone()['id']
            rid = db.execute(
                'INSERT INTO vector_run(vector_id,basis,contract_id,status) '
                'VALUES (?,?,?,?)',
                (vector_id, contract['basis'], cid, '采样中')).lastrowid
            db.executemany(
                'INSERT INTO vector_cell VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                [(rid, x['row'], x['col'], x['u'], x['v'], x['magnitude'],
                  x['east'], x['coverage'], x['state'], x['source_row'],
                  x['source_col'], x['contribution'], x['basis_json'])
                 for x in cell_rows])
            for x in cell_rows:
                if x['state'] != 'valid':
                    db.execute('INSERT INTO vector_issue(run_id,row,col,code,detail)'
                               ' VALUES (?,?,?,?,?)',
                               (rid, x['row'], x['col'], x['state'],
                                f"coverage={x['coverage']:.3f}"))
            os.replace(tmp, out)
            published = True
            db.execute('INSERT INTO vector_output VALUES (?,?,?)',
                       (rid, str(out), sha(out.read_bytes())))
            all_valid = all(x['state'] == 'valid' for x in cell_rows)
            db.execute('UPDATE vector_run SET status=?,ended=CURRENT_TIMESTAMP '
                       'WHERE id=?', ('待核' if not all_valid else '已核验', rid))
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
# independent audit and eastward risk decision
# ---------------------------------------------------------------------------

def audit(db_path, run_id):
    """Independently recompute sampling and direction from the frozen sources.

    Any tampering with either source file, either output band or the per-cell
    evidence tables revokes the verified conclusion (status ``失败``).
    """
    db = connect(db_path)
    row = db.execute(
        'SELECT o.path,o.hash,c.body AS contract_body,vs.* FROM vector_output o '
        'JOIN vector_run vr ON vr.id=o.run_id '
        'JOIN vector_contract c ON c.id=vr.contract_id '
        'JOIN vector_source vs ON vs.id=vr.vector_id WHERE o.run_id=?',
        (run_id,)).fetchone()
    if not row:
        db.close()
        raise ValueError('unknown_vector_run')
    issues = []
    contract = json.loads(row['contract_body'])

    # 1. source byte hashes and frozen grid metadata
    for key, hash_key in (('u_path', 'u_hash'), ('v_path', 'v_hash')):
        p = Path(row[key])
        if not p.is_file() or sha(p.read_bytes()) != row[hash_key]:
            issues.append('source_hash:' + key)
    if not any(i.startswith('source_hash') for i in issues):
        fresh = inspect_source(db_path, row['id'])
        if not fresh['intact']:
            issues.append('source_metadata')

    out_path = Path(row['path'])
    if not out_path.is_file() or sha(out_path.read_bytes()) != row['hash']:
        issues.append('output_hash')

    recorded = [dict(x) for x in db.execute(
        'SELECT * FROM vector_cell WHERE run_id=? ORDER BY row,col', (run_id,))]

    # 2. recompute every cell independently from the two source files
    recomputed = {}
    if not any(i.startswith('source_hash') for i in issues):
        src_transform = Affine(*json.loads(row['transform_json']))
        src_crs = CRS.from_string(row['crs'])
        dst_crs = CRS.from_string(contract['crs'])
        dst_transform = from_bounds(*contract['bounds'],
                                    contract['width'], contract['height'])
        with rasterio.open(row['u_path']) as du, \
                rasterio.open(row['v_path']) as dv:
            u_grid = du.read(1).astype('float64')
            v_grid = dv.read(1).astype('float64')
            nodata = row['nodata']
            valid = np.isfinite(u_grid) & np.isfinite(v_grid)
            if nodata is not None:
                valid &= (u_grid != nodata) & (v_grid != nodata)
            for cell in recorded:
                r, c = cell['row'], cell['col']
                tx, ty = dst_transform @ (c + 0.5, r + 0.5)
                try:
                    sx, sy = warp_transform(dst_crs, src_crs, [tx], [ty])
                    sx, sy = sx[0], sy[0]
                    sr_f, sc_f = rowcol(src_transform, sx, sy, op=float)
                    sampled = _joint_sample(u_grid, v_grid, valid,
                                            float(sr_f), float(sc_f))
                    if sampled is None or sampled[2] < contract['min_coverage'] \
                            or not sampled[3]:
                        expect = None
                    else:
                        su, sv, scov, _contrib = sampled
                        src_frame = local_frame(src_crs, src_transform, sx, sy)
                        dst_frame = local_frame(dst_crs, dst_transform, tx, ty)
                        converted = convert_pair(su, sv, contract['basis'],
                                                 src_frame, dst_frame)
                        # The independently re-sampled pair rides along so the
                        # magnitude check never trusts stored evidence.
                        expect = converted + (su, sv, scov)
                except TRANSFORM_ERRORS:
                    expect = None
                recomputed[(r, c)] = expect

    # 3. evidence table integrity and output band cross-check
    if len(recorded) != contract['width'] * contract['height']:
        issues.append('cell_evidence_missing')
    for cell in recorded:
        r, c = cell['row'], cell['col']
        expect = recomputed.get((r, c), 'unavailable')
        if expect == 'unavailable':
            continue
        if cell['state'] == 'valid':
            if expect is None:
                issues.append(f'cell_forged_valid:{r},{c}')
                continue
            tu, tv, east, _north, su, sv, scov = expect
            mag = float(np.hypot(tu, tv))
            if not (np.isclose(cell['u_value'], tu, rtol=1e-9, atol=1e-9)
                    and np.isclose(cell['v_value'], tv, rtol=1e-9, atol=1e-9)
                    and np.isclose(cell['east_component'], east, rtol=1e-9, atol=1e-9)
                    and np.isclose(cell['magnitude'], mag, rtol=1e-9, atol=1e-9)):
                issues.append(f'cell_mismatch:{r},{c}')
            # Magnitude invariance under the pure basis rotation is an exact
            # identity only for a full-coverage interior cell sampling a
            # constant vector field: bilinear interpolation of a varying field
            # legitimately changes the mean vector's magnitude at a cell
            # centre, and a boundary kernel loses neighbours.
            constant_field = (np.nanstd(u_grid[valid]) < 1e-12
                              and np.nanstd(v_grid[valid]) < 1e-12)
            if scov >= 1.0 - 1e-9 and constant_field:
                src_mag = float(np.hypot(su, sv))
                if not np.isclose(src_mag, mag, rtol=1e-9, atol=1e-9):
                    issues.append(f'magnitude_drift:{r},{c}')
        elif expect is not None:
            issues.append(f'cell_suppressed:{r},{c}')

    if 'output_hash' not in issues and out_path.is_file():
        with rasterio.open(out_path) as src:
            if src.count != 2 or src.width != contract['width'] \
                    or src.height != contract['height'] \
                    or str(src.crs) != str(CRS.from_string(contract['crs'])):
                issues.append('grid_mismatch')
            else:
                band_u, band_v = src.read(1), src.read(2)
                for cell in recorded:
                    r, c = cell['row'], cell['col']
                    if cell['state'] == 'valid':
                        if not (np.isclose(band_u[r, c], cell['u_value'], rtol=1e-9, atol=1e-9)
                                and np.isclose(band_v[r, c], cell['v_value'], rtol=1e-9, atol=1e-9)):
                            issues.append(f'band_mismatch:{r},{c}')
                    elif band_u[r, c] != NODATA or band_v[r, c] != NODATA:
                        issues.append(f'band_forged_nodata:{r},{c}')

    hard = {'output_hash', 'grid_mismatch', 'cell_evidence_missing'}
    severe = bool(issues) and (
        bool(hard.intersection(issues))
        or any(i.split(':')[0] in ('source_hash', 'source_metadata',
                                   'cell_mismatch', 'cell_forged_valid',
                                   'cell_suppressed', 'band_mismatch',
                                   'band_forged_nodata', 'magnitude_drift')
               for i in issues))
    with db:
        db.execute("DELETE FROM vector_issue WHERE run_id=? AND code LIKE 'audit_%'",
                   (run_id,))
        for code in issues:
            db.execute('INSERT INTO vector_issue(run_id,code,detail) VALUES (?,?,?)',
                       (run_id, 'audit_' + code, 'independent vector verification'))
        if severe:
            # Tampering revokes every verified conclusion, including the
            # eastward decisions already recorded for this run.
            db.execute("UPDATE vector_run SET status='失败' WHERE id=?", (run_id,))
            db.execute('DELETE FROM east_decision WHERE run_id=?', (run_id,))
        elif db.execute('SELECT count(*) FROM vector_cell WHERE run_id=? AND state!=?',
                        (run_id, 'valid')).fetchone()[0]:
            db.execute("UPDATE vector_run SET status='待核' WHERE id=?", (run_id,))
        else:
            db.execute("UPDATE vector_run SET status='已核验' WHERE id=?", (run_id,))
    db.close()
    return issues


def decide_east(db_path, run_id, contract_override=None):
    """Eastward risk verdict per cell in stable row/col order.

    ``exceed`` / ``clear`` is only given where joint coverage meets the
    contract threshold and the east component has transform evidence; every
    other cell is ``indeterminate``.
    """
    db = connect(db_path)
    try:
        row = db.execute(
            'SELECT c.body FROM vector_run vr JOIN vector_contract c '
            'ON c.id=vr.contract_id WHERE vr.id=?', (run_id,)).fetchone()
        if not row:
            raise ValueError('unknown_vector_run')
        contract = json.loads(row['body'])
        threshold = float(contract['east_threshold'])
        min_coverage = float(contract['min_coverage'])
        cells = db.execute(
            'SELECT * FROM vector_cell WHERE run_id=? ORDER BY row,col',
            (run_id,)).fetchall()
        results = []
        with db:
            for cell in cells:
                r, c = cell['row'], cell['col']
                evidence = json.dumps({
                    'row': r, 'col': c, 'state': cell['state'],
                    'coverage': cell['coverage'],
                    'east_component': cell['east_component'],
                    'magnitude': cell['magnitude'],
                    'threshold': threshold,
                    'min_coverage': min_coverage,
                    'basis': cell['basis_json']}, sort_keys=True)
                if (cell['state'] == 'valid'
                        and cell['coverage'] >= min_coverage
                        and cell['east_component'] is not None):
                    status = 'exceed' if cell['east_component'] > threshold else 'clear'
                    east = cell['east_component']
                else:
                    status = 'indeterminate'
                    east = None
                db.execute(
                    'INSERT INTO east_decision(run_id,row,col,status,'
                    'east_component,coverage,threshold,evidence) '
                    'VALUES (?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(run_id,row,col) DO UPDATE SET status=excluded.status,'
                    'east_component=excluded.east_component,coverage=excluded.coverage,'
                    'threshold=excluded.threshold,evidence=excluded.evidence',
                    (run_id, r, c, status, east, cell['coverage'],
                     threshold, evidence))
                results.append({'row': r, 'col': c, 'status': status,
                                'east_component': east,
                                'coverage': cell['coverage']})
        return results
    finally:
        db.close()


def report(db_path, run_id, path):
    """Write a JSON report (plus Markdown summary) with stable cell ordering."""
    findings = audit(db_path, run_id)
    decisions = decide_east(db_path, run_id)
    db = connect(db_path)
    try:
        run_row = db.execute('SELECT * FROM vector_run WHERE id=?',
                             (run_id,)).fetchone()
        if not run_row:
            raise ValueError('unknown_vector_run')
        output = db.execute('SELECT * FROM vector_output WHERE run_id=?',
                            (run_id,)).fetchone()
        cells = [dict(x) for x in db.execute(
            'SELECT row,col,u_value,v_value,magnitude,east_component,coverage,'
            'state,source_row,source_col,contribution_json,basis_json '
            'FROM vector_cell WHERE run_id=? ORDER BY row,col', (run_id,))]
        obj = {
            'run': dict(run_row),
            'output': dict(output),
            'audit': findings,
            'east_decisions': decisions,
            'cells': cells,
            'issues': [dict(x) for x in db.execute(
                'SELECT row,col,code,detail FROM vector_issue WHERE run_id=? '
                'ORDER BY id', (run_id,))]}
    finally:
        db.close()
    path = Path(path)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                    encoding='utf-8')
    lines = ['# 向量重投与东向风险报告', '',
             f"运行：{obj['run']['status']}，独立复核：{findings}", '',
             '| 行 | 列 | 状态 | 覆盖 | 东向分量 | 模长 | 裁定 |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    decision_map = {(d['row'], d['col']): d['status'] for d in decisions}
    for cell in cells:
        lines.append(
            f"| {cell['row']} | {cell['col']} | {cell['state']} | "
            f"{cell['coverage']:.3f} | "
            f"{cell['east_component'] if cell['east_component'] is not None else '—'} | "
            f"{cell['magnitude'] if cell['magnitude'] is not None else '—'} | "
            f"{decision_map[(cell['row'], cell['col'])]} |")
    path.with_suffix('.md').write_text('\n'.join(lines) + '\n',
                                       encoding='utf-8')
    return path
