"""Regression tests for paired U/V vector reprojection and eastward rulings."""
import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine, from_bounds

from gridreproject import vector, vectorrun
from gridreproject.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'
ROUTE_GRID = FIX / 'vector-route-source-grid.json'
ROUTE_EN = FIX / 'vector-route-east-north.json'

NORTH_UP = Affine(1, 0, 0, 0, -1, 2)


def contract(basis, **changes):
    obj = {'crs': 'EPSG:4326', 'bounds': [0, 0, 2, 2], 'width': 2, 'height': 2,
           'basis': basis, 'velocity_unit': 'm/s',
           'min_coverage': 0.5, 'east_threshold': 0.5}
    obj.update(changes)
    return obj


def write_pair(tmp_path, u, v, transform, crs='EPSG:4326', nodata=-9999.0,
               uid='pair', basis='source_grid'):
    u_path, v_path = tmp_path / (uid + '-u.tif'), tmp_path / (uid + '-v.tif')
    u, v = np.asarray(u, dtype='float64'), np.asarray(v, dtype='float64')
    for path, data in ((u_path, u), (v_path, v)):
        with rasterio.open(path, 'w', driver='GTiff', width=data.shape[1],
                           height=data.shape[0], count=1, dtype='float64',
                           crs=crs, transform=transform, nodata=nodata) as dst:
            dst.write(data, 1)
    row = vector.register(tmp_path / 'ledger.db', uid, u_path, v_path, basis)
    return row


def decisions_by_cell(rows):
    return {(r['row'], r['col']): r for r in rows}


# ---------------------------------------------------------------------------
# the same rotated file, two legal declarations, opposite business outcomes
# ---------------------------------------------------------------------------

def test_same_rotated_file_two_routes_disagree(tmp_path):
    db = tmp_path / 'ledger.db'
    grid_row = vector.demo(db, tmp_path / 'grid-route', ROUTE_GRID)
    en_row = vector.demo(db, tmp_path / 'en-route', ROUTE_EN)
    # distinct frozen identities, neither reuses the other's evidence
    assert grid_row['id'] != en_row['id']
    assert grid_row['basis'] == 'source_grid'
    assert en_row['basis'] == 'earth_east_north'
    assert grid_row['u_path'] != en_row['u_path']

    grid_rid = vectorrun.run(db, grid_row['id'], contract('source_grid'),
                             tmp_path / 'grid-out.tif')
    en_rid = vectorrun.run(db, en_row['id'], contract('earth_east_north'),
                           tmp_path / 'en-out.tif')
    assert grid_rid != en_rid
    assert vectorrun.audit(db, grid_rid) == []
    assert vectorrun.audit(db, en_rid) == []

    with connect(db) as ledger:
        grid_cells = ledger.execute(
            'SELECT row,col FROM vector_cell WHERE run_id=?', (grid_rid,)).fetchall()
        en_cells = ledger.execute(
            'SELECT row,col FROM vector_cell WHERE run_id=?', (en_rid,)).fetchall()
        assert len(grid_cells) == len(en_cells) == 4
        # decisions are produced on demand, then frozen as separate evidence
        vectorrun.decide_east(db, grid_rid)
        vectorrun.decide_east(db, en_rid)
        assert ledger.execute(
            'SELECT count(*) FROM east_decision WHERE run_id=?',
            (grid_rid,)).fetchone()[0] == 4
        assert ledger.execute(
            'SELECT count(*) FROM east_decision WHERE run_id=?',
            (en_rid,)).fetchone()[0] == 4

    grid_d = decisions_by_cell(vectorrun.decide_east(db, grid_rid))
    en_d = decisions_by_cell(vectorrun.decide_east(db, en_rid))
    # every covered cell: route A points north (east ~ 0, clear),
    # route B points east (east ~ magnitude, exceed where coverage lets it)
    for key in ((0, 1), (1, 0), (1, 1)):
        assert grid_d[key]['status'] == 'clear'
        assert abs(grid_d[key]['east_component']) < 1e-6
        assert en_d[key]['east_component'] > 0.4
    assert en_d[(1, 1)]['status'] == 'exceed'
    # the uncovered corner is indeterminate on both routes, never edge-filled
    assert grid_d[(0, 0)]['status'] == 'indeterminate'
    assert en_d[(0, 0)]['status'] == 'indeterminate'


def test_naive_unrotated_component_mapping_cannot_pass(tmp_path):
    """A stub that pastes U,V straight onto target X/Y gets the rotation wrong."""
    db = tmp_path / 'ledger.db'
    row = vector.demo(db, tmp_path / 'src', ROUTE_GRID)
    rid = vectorrun.run(db, row['id'], contract('source_grid'),
                        tmp_path / 'out.tif')
    with rasterio.open(tmp_path / 'out.tif') as out:
        u_band, v_band = out.read(1), out.read(2)
    # rotated 90 deg source: source column axis is geographic north, which is
    # the target *row* (negative Y) axis, not the target X axis
    with connect(db) as ledger:
        valid = [tuple(r) for r in ledger.execute(
            "SELECT row,col FROM vector_cell WHERE run_id=? AND state='valid'",
            (rid,))]
    assert valid
    for r, c in valid:
        assert abs(u_band[r, c]) < 0.05          # target X component ~ 0
        assert v_band[r, c] < -0.4               # target row component ~ -mag
    # the naive expectation (U -> X) would be u_band ~ 1 and is contradicted
    finite = u_band[u_band != -9999.0]
    assert finite.size and not np.allclose(np.abs(finite), 1.0, atol=0.1)
    assert vectorrun.audit(db, rid) == []


# ---------------------------------------------------------------------------
# joint NoData: one component missing kills the whole vector cell
# ---------------------------------------------------------------------------

def test_u_valid_v_nodata_cell_is_indeterminate(tmp_path):
    db = tmp_path / 'ledger.db'
    # 3x3 rotated source with the centre cell missing V only; the north-up
    # target centre maps exactly onto that source centre
    rot = Affine(0, -1, 3, 1, 0, 0)
    u = np.ones((3, 3))
    v = np.zeros((3, 3))
    v[1, 1] = -9999.0
    row = write_pair(tmp_path, u, v, rot, uid='partial')
    rid = vectorrun.run(db, row['id'],
                        contract('source_grid', width=3, height=3,
                                 bounds=[0, 0, 3, 3], min_coverage=0.0),
                        tmp_path / 'out.tif')
    with connect(db) as ledger:
        cells = {(r['row'], r['col']): dict(r) for r in ledger.execute(
            'SELECT row,col,state,coverage,u_value,v_value FROM vector_cell '
            'WHERE run_id=?', (rid,))}
    # the centre cell maps onto the V-NoData source cell: no inherited scalar
    # validity, no edge fill
    target = cells[(1, 1)]
    assert target['state'] != 'valid'
    assert target['u_value'] is None and target['v_value'] is None
    d = decisions_by_cell(vectorrun.decide_east(db, rid))
    assert d[(1, 1)]['status'] == 'indeterminate'
    with rasterio.open(tmp_path / 'out.tif') as out:
        u_band, v_band = out.read(1), out.read(2)
    assert u_band[1, 1] == -9999.0 and v_band[1, 1] == -9999.0
    # untouched full-kernel interior neighbours stay valid vectors
    assert cells[(0, 1)]['state'] == 'valid'


def test_both_components_nodata_writes_pair_nodata(tmp_path):
    db = tmp_path / 'ledger.db'
    rot = Affine(0, -1, 2, 1, 0, 0)
    row = write_pair(tmp_path,
                     [[1.0, -9999.0], [1.0, 1.0]],
                     [[0.0, -9999.0], [0.0, 0.0]],
                     rot, uid='both-missing')
    rid = vectorrun.run(db, row['id'], contract('source_grid', min_coverage=1.0),
                        tmp_path / 'out.tif')
    with rasterio.open(tmp_path / 'out.tif') as out:
        u_band, v_band = out.read(1), out.read(2)
    d = decisions_by_cell(vectorrun.decide_east(db, rid))
    # any cell whose neighbourhood touches the NoData fails the strict mask
    touched = [k for k, v in d.items() if v['status'] == 'indeterminate']
    assert touched
    for r, c in touched:
        assert u_band[r, c] == -9999.0 and v_band[r, c] == -9999.0


# ---------------------------------------------------------------------------
# unrotated grid, magnitude invariance and same-CRS identity
# ---------------------------------------------------------------------------

def test_unrotated_grid_preserves_components_and_magnitude(tmp_path):
    db = tmp_path / 'ledger.db'
    # constant vector field (3, 4): every full-coverage interior cell must
    # keep the pair and the physical magnitude 5 under a pure basis rotation
    row = write_pair(tmp_path,
                     [[3.0, 3.0], [3.0, 3.0]],
                     [[4.0, 4.0], [4.0, 4.0]],
                     NORTH_UP, uid='flat')
    rid = vectorrun.run(db, row['id'],
                        contract('source_grid', min_coverage=0.0),
                        tmp_path / 'out.tif')
    assert vectorrun.audit(db, rid) == []
    with rasterio.open(tmp_path / 'out.tif') as out:
        u_band, v_band = out.read(1), out.read(2)
    assert np.isclose(u_band[0, 0], 3.0)
    assert np.isclose(v_band[0, 0], 4.0)
    with connect(db) as ledger:
        cell = dict(ledger.execute(
            'SELECT * FROM vector_cell WHERE run_id=? AND row=0 AND col=0',
            (rid,)).fetchone())
    assert np.isclose(cell['magnitude'], 5.0)
    assert cell['coverage'] >= 1.0 - 1e-9


def test_earth_east_north_basis_on_northup_grid(tmp_path):
    db = tmp_path / 'ledger.db'
    row = write_pair(tmp_path,
                     [[1.0, 1.0], [1.0, 1.0]],
                     [[0.0, 0.0], [0.0, 0.0]],
                     NORTH_UP, uid='en-flat', basis='earth_east_north')
    rid = vectorrun.run(db, row['id'],
                        contract('earth_east_north', min_coverage=0.0),
                        tmp_path / 'out.tif')
    d = decisions_by_cell(vectorrun.decide_east(db, rid))
    # boundary cells keep sub-unit kernel coverage but the constant east
    # vector still projects onto the target X axis, so every cell exceeds
    assert all(v['status'] == 'exceed' for v in d.values())
    with rasterio.open(tmp_path / 'out.tif') as out:
        u_band = out.read(1)
    valid = u_band[u_band != -9999.0]
    assert np.allclose(valid, 1.0, atol=1e-6)


# ---------------------------------------------------------------------------
# cross-CRS: pure rotation keeps the physical magnitude
# ---------------------------------------------------------------------------

def test_cross_crs_reprojection_keeps_magnitude(tmp_path):
    db = tmp_path / 'ledger.db'
    row = write_pair(tmp_path,
                     [[1.0, 1.0], [1.0, 1.0]],
                     [[1.0, 1.0], [1.0, 1.0]],
                     NORTH_UP, uid='cross')
    target = contract('source_grid', crs='EPSG:3857',
                      bounds=[0.0, 0.0, 222684.2085, 222684.2085],
                      min_coverage=0.2)
    rid = vectorrun.run(db, row['id'], target, tmp_path / 'out.tif')
    assert vectorrun.audit(db, rid) == []
    with connect(db) as ledger:
        cells = [dict(r) for r in ledger.execute(
            "SELECT * FROM vector_cell WHERE run_id=? AND state='valid'",
            (rid,))]
    assert cells
    src_mag = float(np.hypot(1.0, 1.0))
    for cell in cells:
        assert np.isclose(cell['magnitude'], src_mag, rtol=1e-6, atol=1e-6)
        assert np.isfinite(cell['east_component'])


# ---------------------------------------------------------------------------
# boundary coverage and undefined local transforms
# ---------------------------------------------------------------------------

def test_target_outside_source_is_indeterminate_never_filled(tmp_path):
    db = tmp_path / 'ledger.db'
    row = write_pair(tmp_path, [[1.0, 1.0], [1.0, 1.0]],
                     [[0.0, 0.0], [0.0, 0.0]], NORTH_UP, uid='edge')
    rid = vectorrun.run(db, row['id'],
                        contract('source_grid', bounds=[100, 100, 102, 102]),
                        tmp_path / 'out.tif')
    with rasterio.open(tmp_path / 'out.tif') as out:
        u_band, v_band = out.read(1), out.read(2)
    assert np.all(u_band == -9999.0) and np.all(v_band == -9999.0)
    d = vectorrun.decide_east(db, rid)
    assert all(x['status'] == 'indeterminate' for x in d)
    with connect(db) as ledger:
        assert ledger.execute(
            "SELECT count(*) FROM vector_cell WHERE run_id=? AND state='valid'",
            (rid,)).fetchone()[0] == 0


def test_undefined_local_transform_marked_indeterminate(tmp_path):
    db = tmp_path / 'ledger.db'
    # source in UTM 33N; a target cell centre at longitude 999 cannot be
    # transformed, so the direction basis is undefined there
    row = write_pair(tmp_path, [[1.0, 1.0], [1.0, 1.0]],
                     [[0.0, 0.0], [0.0, 0.0]], NORTH_UP,
                     crs='EPSG:32633', uid='utm')
    bad = contract('source_grid', crs='EPSG:4326',
                   bounds=[998.0, 0.0, 1000.0, 2.0], min_coverage=0.0)
    rid = vectorrun.run(db, row['id'], bad, tmp_path / 'out.tif')
    with connect(db) as ledger:
        states = {r['state'] for r in ledger.execute(
            'SELECT DISTINCT state FROM vector_cell WHERE run_id=?', (rid,))}
    assert any(s.startswith('indeterminate') for s in states)
    assert 'valid' not in states
    d = vectorrun.decide_east(db, rid)
    assert all(x['status'] == 'indeterminate' for x in d)
    with rasterio.open(tmp_path / 'out.tif') as out:
        assert np.all(out.read(1) == -9999.0)
        assert np.all(out.read(2) == -9999.0)


# ---------------------------------------------------------------------------
# tamper evidence: sources, output bands, evidence tables
# ---------------------------------------------------------------------------

def _clean_run(tmp_path, uid='tamper', basis='source_grid'):
    db = tmp_path / 'ledger.db'
    # constant field keeps the clean-run audit free of interpolation effects
    row = write_pair(tmp_path, [[2.0, 2.0], [2.0, 2.0]],
                     [[1.0, 1.0], [1.0, 1.0]], NORTH_UP, uid=uid, basis=basis)
    rid = vectorrun.run(db, row['id'], contract(basis, min_coverage=0.0),
                        tmp_path / (uid + '.tif'))
    assert vectorrun.audit(db, rid) == []
    return db, row, rid


def test_source_tampering_revokes_vector_conclusion(tmp_path):
    db, row, rid = _clean_run(tmp_path, uid='src-tamper')
    Path(row['u_path']).write_bytes(b'changed-bytes')
    issues = vectorrun.audit(db, rid)
    assert any(i.startswith('source_hash') for i in issues)
    with connect(db) as ledger:
        assert ledger.execute('SELECT status FROM vector_run WHERE id=?',
                              (rid,)).fetchone()[0] == '失败'
        # eastward verdict is revoked together with the verified conclusion
        assert ledger.execute('SELECT count(*) FROM east_decision WHERE run_id=?',
                              (rid,)).fetchone()[0] == 0


def test_output_band_tampering_detected(tmp_path):
    db, _row, rid = _clean_run(tmp_path, uid='band-tamper')
    out = tmp_path / 'band-tamper.tif'
    with rasterio.open(out) as src:
        profile, u_band, v_band = src.profile, src.read(1), src.read(2)
    u_band[0, 0] += 10.0
    with rasterio.open(out, 'w', **profile) as dst:
        dst.write(u_band, 1)
        dst.write(v_band, 2)
    issues = vectorrun.audit(db, rid)
    assert any(i.startswith('band_mismatch') or i == 'output_hash' for i in issues)
    with connect(db) as ledger:
        assert ledger.execute('SELECT status FROM vector_run WHERE id=?',
                              (rid,)).fetchone()[0] == '失败'


def test_forged_band_with_updated_hash_still_caught(tmp_path):
    db, _row, rid = _clean_run(tmp_path, uid='forge')
    out = tmp_path / 'forge.tif'
    with rasterio.open(out) as src:
        profile, bands = src.profile, src.read()
    bands[1, 0, 0] += 10.0  # second band (V) forgery
    with rasterio.open(out, 'w', **profile) as dst:
        dst.write(bands)
    with connect(db) as ledger:
        ledger.execute('UPDATE vector_output SET hash=? WHERE run_id=?',
                       (vectorrun.sha(out.read_bytes()), rid))
    issues = vectorrun.audit(db, rid)
    assert any(i.startswith('band_mismatch') for i in issues)


def test_evidence_table_tampering_revokes_run(tmp_path):
    db, _row, rid = _clean_run(tmp_path, uid='cell-tamper')
    with connect(db) as ledger:
        ledger.execute(
            "UPDATE vector_cell SET u_value=999.0 WHERE run_id=? AND row=0 AND col=0",
            (rid,))
    issues = vectorrun.audit(db, rid)
    assert any(i.startswith('cell_mismatch') for i in issues)
    with connect(db) as ledger:
        assert ledger.execute('SELECT status FROM vector_run WHERE id=?',
                              (rid,)).fetchone()[0] == '失败'


def test_nodata_cell_forged_valid_in_table_is_caught(tmp_path):
    db = tmp_path / 'ledger.db'
    rot = Affine(0, -1, 3, 1, 0, 0)
    u = np.ones((3, 3))
    v = np.zeros((3, 3))
    v[1, 1] = -9999.0
    row = write_pair(tmp_path, u, v, rot, uid='forge-valid')
    rid = vectorrun.run(db, row['id'],
                        contract('source_grid', width=3, height=3,
                                 bounds=[0, 0, 3, 3], min_coverage=0.0),
                        tmp_path / 'out.tif')
    with connect(db) as ledger:
        ledger.execute(
            "UPDATE vector_cell SET state='valid',u_value=1.0,v_value=0.0,"
            "magnitude=1.0,east_component=0.0,coverage=1.0 "
            "WHERE run_id=? AND row=1 AND col=1", (rid,))
    issues = vectorrun.audit(db, rid)
    assert any(i.startswith('cell_forged_valid') for i in issues)


# ---------------------------------------------------------------------------
# contract validation and output conflict
# ---------------------------------------------------------------------------

def test_contract_rejects_bad_basis_and_threshold(tmp_path):
    db = tmp_path / 'ledger.db'
    with pytest.raises(ValueError, match='invalid_vector_basis'):
        vectorrun.plan(db, contract('compass'))
    with pytest.raises(ValueError, match='missing_vector_contract'):
        vectorrun.plan(db, {'crs': 'EPSG:4326'})
    with pytest.raises(ValueError, match='bad_east_threshold'):
        vectorrun.plan(db, contract('source_grid', east_threshold=float('nan')))


def test_basis_mismatch_between_source_and_contract_blocks_run(tmp_path):
    db = tmp_path / 'ledger.db'
    row = vector.demo(db, tmp_path / 'src', ROUTE_GRID)  # source_grid
    with pytest.raises(ValueError, match='basis_contract_conflict'):
        vectorrun.run(db, row['id'], contract('earth_east_north'),
                      tmp_path / 'out.tif')


def test_vector_output_conflict_keeps_existing_file(tmp_path):
    db = tmp_path / 'ledger.db'
    row = write_pair(tmp_path, [[1.0], [1.0]], [[0.0], [0.0]],
                     Affine(1, 0, 0, 0, -1, 1), uid='conflict')
    out = tmp_path / 'out.tif'
    out.write_bytes(b'keep-me')
    with pytest.raises(ValueError, match='vector_output_exists'):
        vectorrun.run(db, row['id'],
                      contract('source_grid', width=1, height=1,
                               bounds=[0, 0, 1, 1]), out)
    assert out.read_bytes() == b'keep-me'


# ---------------------------------------------------------------------------
# report and stable cell ordering
# ---------------------------------------------------------------------------

def test_report_lists_cells_in_stable_order_with_evidence(tmp_path):
    db = tmp_path / 'ledger.db'
    row = vector.demo(db, tmp_path / 'src', ROUTE_GRID)
    rid = vectorrun.run(db, row['id'], contract('source_grid'),
                        tmp_path / 'out.tif')
    path = vectorrun.report(db, rid, tmp_path / 'report.json')
    obj = json.loads(path.read_text(encoding='utf-8'))
    keys = [(c['row'], c['col']) for c in obj['cells']]
    assert keys == sorted(keys)
    assert len(obj['east_decisions']) == 4
    valid_cell = next(c for c in obj['cells'] if c['state'] == 'valid')
    assert json.loads(valid_cell['basis_json'])['basis'] == 'source_grid'
    contrib = json.loads(valid_cell['contribution_json'])
    assert contrib and all('u' in x and 'v' in x and 'weight' in x
                           for x in contrib)
    assert path.with_suffix('.md').is_file()
