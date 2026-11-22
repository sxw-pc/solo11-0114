"""Frozen control-point correction: fitting, inverse localization and audit."""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from gridreproject import control, engine, geometry
from gridreproject.store import connect


# --------------------------------------------------------------------------- #
# Synthetic fixtures
# --------------------------------------------------------------------------- #
def write_raster(path, values, width=None, height=None, nodata=-9999.0,
                 crs='EPSG:4326'):
    values = np.asarray(values, dtype='float64')
    height, width = values.shape
    with rasterio.open(path, 'w', driver='GTiff', width=width, height=height,
                       count=1, dtype='float64', crs=crs,
                       transform=from_bounds(0, 0, width, height, width, height),
                       nodata=nodata) as dst:
        dst.write(values, 1)
    return path


def register_survey(db, raster, survey_id, points, target_crs='EPSG:4326',
                    root=None):
    root = root or raster.parent
    obs = root / f'{survey_id}.json'
    obs.write_text(json.dumps({'target_crs': target_crs, 'points': points}))
    return control.register(db, survey_id, raster, obs)


def corner_shift_points(shift=0.5, n=4):
    """GCPs whose measured targets sit ``shift`` east of the file transform."""
    pts = []
    for k, (r, c) in enumerate([(0.5, 0.5), (0.5, n - 0.5),
                                (n - 0.5, 0.5), (n - 0.5, n - 0.5)]):
        pts.append({'id': f'G{k}', 'source_row': r, 'source_col': c,
                    'target_x': c + shift, 'target_y': n - r,
                    'weight': 1.0})
    return pts


def contract(**changes):
    obj = {'crs': 'EPSG:4326', 'bounds': [0, 0, 6, 6], 'width': 6, 'height': 6,
           'kind': 'category', 'method': 'nearest', 'min_coverage': 1.0}
    obj.update(changes)
    return obj


def bumpy_mesh_points(bump=1.0):
    """3x3 node grid; the center node is displaced one source pixel east."""
    pts = []
    for i, r in enumerate([0.5, 3.5, 5.5]):
        for j, c in enumerate([0.5, 3.5, 5.5]):
            x, y = c, 5.5 - r
            if i == 1 and j == 1:
                x += bump
            pts.append({'id': f'G{i}{j}', 'source_row': r, 'source_col': c,
                        'target_x': x, 'target_y': y, 'weight': 1.0})
    return pts


# --------------------------------------------------------------------------- #
# geometry module unit checks
# --------------------------------------------------------------------------- #
def test_affine_recovers_exact_similarity_and_residuals():
    pts = corner_shift_points(0.5)
    model, records = geometry.fit_global_affine(pts, 1e-9)
    assert model.ax == pytest.approx(1.0, abs=1e-10)
    assert model.cx == pytest.approx(0.5, abs=1e-10)
    assert model.by == pytest.approx(-1.0, abs=1e-10)
    assert all(rec['residual'] < 1e-9 and rec['accepted'] for rec in records)
    row, col = model.inverse(2.5, 2.0)
    assert col == pytest.approx(2.0, abs=1e-10)
    assert row == pytest.approx(2.0, abs=1e-10)


def test_degenerate_point_layout_is_refused():
    collinear = [{'id': str(i), 'source_row': 1.0, 'source_col': float(i),
                  'target_x': float(i), 'target_y': 1.0, 'weight': 1.0}
                 for i in range(4)]
    with pytest.raises(geometry.GeometryError, match='degenerate_control_matrix'):
        geometry.fit_global_affine(collinear, 1.0)


def test_mesh_requires_rectangular_grid():
    pts = [{'id': str(i), 'source_row': 0.5, 'source_col': float(i) + 0.5,
            'target_x': float(i), 'target_y': 1.0, 'weight': 1.0}
           for i in range(4)]
    with pytest.raises(geometry.GeometryError, match='mesh_layout_invalid'):
        geometry.fit_local_mesh(pts, 1.0)


def test_mesh_fold_is_detected():
    pts = bumpy_mesh_points()
    # Reversing two adjacent node targets flips the quad orientation.
    pts[3]['target_x'], pts[4]['target_x'] = pts[4]['target_x'], pts[3]['target_x']
    with pytest.raises(geometry.GeometryError, match='mesh_folded'):
        geometry.fit_local_mesh(pts, 1.0)


def test_mesh_never_extrapolates_beyond_nodes():
    model, _ = geometry.fit_local_mesh(bumpy_mesh_points(), 1.0)
    with pytest.raises(geometry.GeometryError, match='point_outside_mesh'):
        model.inverse(-5.0, -5.0)


def test_inverse_non_convergence_has_stable_code(monkeypatch):
    import dataclasses
    model, _ = geometry.fit_local_mesh(bumpy_mesh_points(), 1.0)
    # Zero iterations: the first Newton step immediately leaves the quad, so
    # the solver refuses rather than returning an unconverged location.
    model = dataclasses.replace(model, max_iter=0)
    with pytest.raises(geometry.GeometryError,
                       match='inverse_left_quad|inverse_not_converged'):
        model.inverse(3.5, 2.0)
    # A point beyond every quad always reports the boundary code.
    with pytest.raises(geometry.GeometryError, match='point_outside_mesh'):
        geometry.fit_local_mesh(bumpy_mesh_points(), 1.0)[0].inverse(-9, -9)


def test_affine_fold_uses_singular_value_gate():
    pts = [{'id': 'a', 'source_row': 0.0, 'source_col': 0.0,
            'target_x': 0.0, 'target_y': 0.0, 'weight': 1.0},
           {'id': 'b', 'source_row': 0.0, 'source_col': 1.0,
            'target_x': 1.0, 'target_y': 0.0, 'weight': 1.0},
           {'id': 'c', 'source_row': 1.0, 'source_col': 0.0,
            'target_x': 2.0, 'target_y': 0.0, 'weight': 1.0}]
    with pytest.raises(geometry.GeometryError, match='control_geometry_folded'):
        geometry.fit_global_affine(pts, 1.0)


# --------------------------------------------------------------------------- #
# Engine: corrected runs
# --------------------------------------------------------------------------- #
def test_correction_never_silently_falls_back_to_file_transform(tmp_path):
    # The GeoTIFF advertises a deliberately wrong origin (100..104); the frozen
    # GCPs say truth starts at the origin with a +0.5 east shift.
    raster = tmp_path / 'biased.tif'
    with rasterio.open(raster, 'w', driver='GTiff', width=4, height=4, count=1,
                       dtype='float64', crs='EPSG:4326',
                       transform=from_bounds(100, 100, 104, 104, 4, 4),
                       nodata=-9999.0) as dst:
        dst.write(np.tile(np.arange(4.0), (4, 1)), 1)
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'truth',
                    corner_shift_points(0.5, n=4), root=tmp_path)
    cfg = contract(bounds=[0.5, 0.5, 4.5, 4.5], width=4, height=4,
                   survey_id='truth', correction_model='global_affine',
                   control_residual_limit=1e-6)
    rid = engine.run(db, raster, cfg, tmp_path / 'out.tif')
    assert engine.audit(db, rid) == []
    cols = [r['source_col'] for r in connect(db).execute(
        'SELECT source_col FROM cell WHERE run_id=? AND row=0 ORDER BY col',
        (rid,))]
    # Inverse locations follow the GCPs (~0.5, 1.5, ...), never the 100-origin.
    assert cols == pytest.approx([0.5, 1.5, 2.5, 3.5], abs=1e-8)


def test_global_affine_shift_corrects_every_cell(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(4.0), (4, 1)))
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'shift', corner_shift_points(0.5), root=tmp_path)
    cfg = contract(bounds=[0, 0, 4, 4], width=4, height=4,
                   survey_id='shift', correction_model='global_affine',
                   control_residual_limit=1e-6)
    rid = engine.run(db, raster, cfg, tmp_path / 'out.tif')
    assert engine.audit(db, rid) == []
    rows = connect(db).execute(
        'SELECT row,col,source_col FROM cell WHERE run_id=? ORDER BY row,col',
        (rid,)).fetchall()
    # Target center (c+1.0) inverts through the +0.5 shift onto source col c.
    assert all(abs(r['source_col'] - r['col']) < 1e-8 for r in rows)
    with rasterio.open(tmp_path / 'out.tif') as src:
        assert np.array_equal(src.read(1)[0], np.arange(4.0))


def test_two_models_locate_same_target_cell_in_different_columns(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))  # value == col
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'bump', bumpy_mesh_points(), root=tmp_path)
    # One target cell centered on the bumped location (4.0, 2.0): its four
    # corner GCPs agree with the file transform while the center GCP records a
    # verifiable one-pixel east displacement.
    cell_grid = contract(bounds=[3.5, 1.5, 4.5, 2.5], width=1, height=1)
    cfg_aff = dict(cell_grid, survey_id='bump',
                   correction_model='global_affine', control_residual_limit=1.0)
    cfg_mesh = dict(cell_grid, survey_id='bump',
                    correction_model='local_mesh', control_residual_limit=1.0)
    rid_aff = engine.run(db, raster, cfg_aff, tmp_path / 'aff.tif')
    rid_mesh = engine.run(db, raster, cfg_mesh, tmp_path / 'mesh.tif')
    assert engine.audit(db, rid_aff) == []
    assert engine.audit(db, rid_mesh) == []
    d = connect(db)
    aff_cell = d.execute('SELECT source_col,value FROM cell WHERE run_id=?',
                         (rid_aff,)).fetchone()
    mesh_cell = d.execute('SELECT source_col,value FROM cell WHERE run_id=?',
                          (rid_mesh,)).fetchone()
    # Global affine absorbs the local bump into a whole-grid shift: col 4.
    # The mesh preserves the local displacement and stays on source col 3.
    assert round(aff_cell['source_col']) == 4
    assert round(mesh_cell['source_col']) == 3
    assert aff_cell['value'] == 4.0 and mesh_cell['value'] == 3.0
    with rasterio.open(tmp_path / 'aff.tif') as src:
        assert src.read(1)[0, 0] == 4.0
    with rasterio.open(tmp_path / 'mesh.tif') as src:
        assert src.read(1)[0, 0] == 3.0
    # The mesh basis records which quad and parameters produced the location.
    basis = json.loads(d.execute('SELECT basis FROM cell_basis WHERE run_id=?',
                                 (rid_mesh,)).fetchone()['basis'])
    assert basis['kind'] == 'mesh_inverse' and 0 <= basis['u'] <= 1


def test_folded_mesh_is_rejected_end_to_end(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))
    db = tmp_path / 'ledger.db'
    folded = bumpy_mesh_points()
    folded[3]['target_x'], folded[4]['target_x'] = (
        folded[4]['target_x'], folded[3]['target_x'])
    register_survey(db, raster, 'fold', folded, root=tmp_path)
    with pytest.raises(ValueError, match='mesh_folded'):
        engine.run(db, raster,
                   contract(survey_id='fold', correction_model='local_mesh',
                            control_residual_limit=1.0),
                   tmp_path / 'out.tif')
    assert not (tmp_path / 'out.tif').exists()
    assert connect(db).execute(
        "SELECT error_code FROM control_fit WHERE status='拒绝'").fetchone()[
        'error_code'] == 'mesh_folded'


def test_degenerate_affine_layout_is_rejected_end_to_end(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))
    db = tmp_path / 'ledger.db'
    collinear = [{'id': str(i), 'source_row': 1.0, 'source_col': float(i) + 0.5,
                  'target_x': float(i), 'target_y': 1.0, 'weight': 1.0}
                 for i in range(4)]
    register_survey(db, raster, 'line', collinear, root=tmp_path)
    with pytest.raises(ValueError, match='degenerate_control_matrix'):
        engine.run(db, raster,
                   contract(survey_id='line', correction_model='global_affine',
                            control_residual_limit=1.0),
                   tmp_path / 'out.tif')


def test_residual_limit_rejects_affine_but_not_mesh(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'bump', bumpy_mesh_points(), root=tmp_path)
    with pytest.raises(ValueError, match='residual_limit_exceeded'):
        engine.run(db, raster,
                   contract(survey_id='bump', correction_model='global_affine',
                            control_residual_limit=0.1),
                   tmp_path / 'tight.tif')
    assert not (tmp_path / 'tight.tif').exists()
    # Rejection is frozen with per-point residuals and reasons.
    d = connect(db)
    rejected = d.execute("SELECT * FROM control_fit WHERE status='拒绝'").fetchone()
    assert rejected['error_code'] == 'residual_limit_exceeded'
    assert rejected['observation_digest'] == d.execute(
        'SELECT digest FROM control_survey WHERE id=?', ('bump',)).fetchone()['digest']
    pts = d.execute('SELECT * FROM control_fit_point WHERE fit_id=?',
                    (rejected['id'],)).fetchall()
    assert any(p['accepted'] == 0 and p['reject_reason'] == 'residual_limit_exceeded'
               for p in pts)
    assert all(p['weight'] == 1.0 for p in pts)
    # Mesh interpolates the nodes exactly, so the same limit passes.
    rid = engine.run(db, raster,
                     contract(survey_id='bump', correction_model='local_mesh',
                              control_residual_limit=0.1),
                     tmp_path / 'mesh.tif')
    assert engine.audit(db, rid) == []


def test_source_hash_mismatch_blocks_correction(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'bump', bumpy_mesh_points(), root=tmp_path)
    # Rewrite the raster with different bytes; the frozen survey no longer
    # belongs to this source.
    write_raster(raster, np.tile(np.arange(6.0) * 2, (6, 1)))
    with pytest.raises(ValueError, match='control_source_hash_mismatch'):
        engine.run(db, raster,
                   contract(survey_id='bump', correction_model='local_mesh',
                            control_residual_limit=1.0),
                   tmp_path / 'out.tif')


def test_unknown_survey_and_bad_contract_are_rejected(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))
    db = tmp_path / 'ledger.db'
    with pytest.raises(ValueError, match='unknown_control_survey'):
        engine.run(db, raster,
                   contract(survey_id='missing', correction_model='local_mesh',
                            control_residual_limit=1.0),
                   tmp_path / 'out.tif')
    with pytest.raises(ValueError, match='correction_contract_invalid'):
        engine.plan(db, contract(survey_id='missing',
                                 correction_model='made_up_model',
                                 control_residual_limit=1.0))
    with pytest.raises(ValueError, match='correction_contract_invalid'):
        engine.plan(db, contract(survey_id='missing',
                                 correction_model='global_affine',
                                 control_residual_limit=0.0))


def test_unsurveyed_contract_keeps_legacy_outputs(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.array([[1.0, 2.0], [3.0, 4.0]]))
    db = tmp_path / 'ledger.db'
    cfg = {'crs': 'EPSG:4326', 'bounds': [0, 0, 2, 2], 'width': 2, 'height': 2,
           'kind': 'continuous', 'method': 'bilinear', 'min_coverage': 1.0}
    rid = engine.run(db, raster, cfg, tmp_path / 'plain.tif')
    assert engine.audit(db, rid) == []
    assert connect(db).execute(
        'SELECT count(*) FROM control_fit').fetchone()[0] == 0


def test_untransformable_contract_crs_is_rejected(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'bump', bumpy_mesh_points(), root=tmp_path)
    # A geographic survey cannot be expressed in an equatorial-geostationary
    # grid centred over the opposite hemisphere; the transform path fails.
    geostat = ('PROJCS["unnamed",GEOGCS["GRS 1980(IUGG, 1980)",DATUM["D_unknown",'
               'SPHEROID["GRS80",6378137,298.257222101]],PRIMEM["Greenwich",0],'
               'UNIT["degree",0.0174532925199433]],PROJECTION["Geostationary_Satellite"],'
               'PARAMETER["central_meridian",-140],PARAMETER["satellite_height",35785831],'
               'PARAMETER["false_easting",0],PARAMETER["false_northing",0],'
               'UNIT["Meter",1]]')
    cfg = contract(crs=geostat, bounds=[0, 0, 1000, 1000], width=4, height=4,
                   survey_id='bump', correction_model='local_mesh',
                   control_residual_limit=1.0)
    with pytest.raises(ValueError, match='control_crs_not_transformable'):
        engine.run(db, raster, cfg, tmp_path / 'out.tif')
    assert not (tmp_path / 'out.tif').exists()


def test_deleted_correction_evidence_breaks_verification(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'bump', bumpy_mesh_points(), root=tmp_path)
    rid = engine.run(
        db, raster,
        contract(survey_id='bump', correction_model='local_mesh',
                 control_residual_limit=1.0),
        tmp_path / 'out.tif')
    with connect(db) as conn:
        conn.execute('DELETE FROM cell_basis WHERE run_id=?', (rid,))
    assert 'control_evidence_missing' in engine.audit(db, rid)
    assert connect(db).execute('SELECT status FROM run WHERE id=?',
                               (rid,)).fetchone()[0] == '失败'


def test_nodata_target_cell_is_not_filled_from_edge(tmp_path):
    values = np.tile(np.arange(6.0), (6, 1))
    values[:, 2] = -9999.0
    raster = tmp_path / 'src.tif'
    write_raster(raster, values)
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'shift', corner_shift_points(0.5, n=6),
                    root=tmp_path)
    cfg = contract(bounds=[0, 0, 6, 6], width=6, height=6,
                   kind='category', method='nearest', min_coverage=1.0,
                   survey_id='shift', correction_model='global_affine',
                   control_residual_limit=1e-6)
    rid = engine.run(db, raster, cfg, tmp_path / 'out.tif')
    d = connect(db)
    bad = d.execute("SELECT row,col FROM cell WHERE run_id=? AND value IS NULL",
                    (rid,)).fetchall()
    assert (2, 2) in {(r['row'], r['col']) for r in bad}
    issue_codes = {r['code'] for r in d.execute(
        'SELECT code FROM issue WHERE run_id=?', (rid,))}
    assert 'coverage_low' in issue_codes
    with rasterio.open(tmp_path / 'out.tif') as src:
        assert src.read(1)[2, 2] == src.nodata
    assert d.execute('SELECT status FROM run WHERE id=?', (rid,)).fetchone()[0] == '待核'


def test_mesh_boundary_targets_are_unlocatable_not_extrapolated(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'bump', bumpy_mesh_points(), root=tmp_path)
    # Target grid extends beyond the surveyed target hull (nodes span 0.5..6.0
    # in x and 0.0..5.0 in y).
    cfg = contract(bounds=[0, 0, 8, 8], width=8, height=8,
                   survey_id='bump', correction_model='local_mesh',
                   control_residual_limit=1.0)
    rid = engine.run(db, raster, cfg, tmp_path / 'out.tif')
    d = connect(db)
    codes = {r['code'] for r in d.execute(
        'SELECT code FROM issue WHERE run_id=?', (rid,))}
    assert 'unlocatable' in codes
    basis = d.execute(
        "SELECT reason FROM cell_basis WHERE run_id=? AND status='不可定位'",
        (rid,)).fetchall()
    # Both mesh-boundary codes mean "refused, never extrapolated".
    assert basis and all(r['reason'] in
                         ('point_outside_mesh', 'inverse_left_quad',
                          'inverse_not_converged', 'mesh_folded')
                         for r in basis)
    assert d.execute("SELECT count(*) FROM cell_basis WHERE run_id=? AND status='可定位' "
                     "AND (source_row<? OR source_row>? OR source_col<? OR source_col>?)",
                     (rid, 0.5, 5.5, 0.5, 5.5)).fetchone()[0] == 0
    # No edge value leaked into the unlocatable cells.
    with rasterio.open(tmp_path / 'out.tif') as src:
        nodata = src.nodata
        outside = src.read(1)[7, 7]
    assert outside == nodata


def test_corrected_bilinear_continuous_sampling(tmp_path):
    values = np.tile(np.arange(6.0), (6, 1))
    raster = tmp_path / 'src.tif'
    write_raster(raster, values)
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'bump', bumpy_mesh_points(), root=tmp_path)
    cfg = contract(kind='continuous', method='bilinear', min_coverage=0.0,
                   survey_id='bump', correction_model='local_mesh',
                   control_residual_limit=1.0)
    rid = engine.run(db, raster, cfg, tmp_path / 'out.tif')
    assert engine.audit(db, rid) == []
    d = connect(db)
    cell = d.execute('SELECT source_col,value FROM cell WHERE run_id=? AND row=2 AND col=3',
                     (rid,)).fetchone()
    # Bilinear value follows the fractional source column, not a rounded one.
    assert cell['value'] == pytest.approx(cell['source_col'], abs=1e-9)


def test_corrected_extensive_area_sampling_conserves_interior_total(tmp_path):
    values = np.ones((6, 6), dtype='float64')
    raster = tmp_path / 'src.tif'
    write_raster(raster, values)
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'shift', corner_shift_points(0.0, n=6),
                    root=tmp_path)
    cfg = contract(kind='extensive', method='area', min_coverage=0.99,
                   survey_id='shift', correction_model='global_affine',
                   control_residual_limit=1e-6)
    rid = engine.run(db, raster, cfg, tmp_path / 'out.tif')
    issues = engine.audit(db, rid)
    assert issues == []
    with rasterio.open(tmp_path / 'out.tif') as src:
        data = src.read(1)
    finite = data[data != src.nodata]
    # Identity correction: each fully covered target cell integrates one unit.
    assert np.allclose(finite, 1.0, atol=2e-3)


def test_contract_identity_changes_with_correction_version(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'bump', bumpy_mesh_points(), root=tmp_path)
    h1 = engine.plan(db, contract(survey_id='bump',
                                  correction_model='global_affine',
                                  control_residual_limit=1.0))
    h2 = engine.plan(db, contract(survey_id='bump',
                                  correction_model='global_affine',
                                  control_residual_limit=0.5))
    h3 = engine.plan(db, contract(survey_id='bump',
                                  correction_model='local_mesh',
                                  control_residual_limit=1.0))
    assert len({h1, h2, h3}) == 3


def test_failed_correction_preserves_verified_run_and_output(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'bump', bumpy_mesh_points(), root=tmp_path)
    good_rid = engine.run(
        db, raster,
        contract(survey_id='bump', correction_model='local_mesh',
                 control_residual_limit=1.0),
        tmp_path / 'good.tif')
    assert engine.audit(db, good_rid) == []
    original_bytes = (tmp_path / 'good.tif').read_bytes()
    before_runs = connect(db).execute('SELECT count(*) FROM run').fetchone()[0]
    with pytest.raises(ValueError, match='residual_limit_exceeded'):
        engine.run(db, raster,
                   contract(survey_id='bump', correction_model='global_affine',
                            control_residual_limit=0.01),
                   tmp_path / 'bad.tif')
    d = connect(db)
    assert d.execute('SELECT count(*) FROM run').fetchone()[0] == before_runs
    assert d.execute('SELECT count(*) FROM output').fetchone()[0] == 1
    assert (tmp_path / 'good.tif').read_bytes() == original_bytes
    assert not (tmp_path / 'bad.tif').exists()
    assert engine.audit(db, good_rid) == []


def test_tampered_observations_break_verification(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'bump', bumpy_mesh_points(), root=tmp_path)
    rid = engine.run(
        db, raster,
        contract(survey_id='bump', correction_model='global_affine',
                 control_residual_limit=1.0),
        tmp_path / 'out.tif')
    with connect(db) as conn:
        conn.execute("UPDATE control_point SET target_x=target_x+0.25 WHERE survey_id='bump'")
    assert 'control_observation_tampered' in engine.audit(db, rid)
    assert connect(db).execute('SELECT status FROM run WHERE id=?',
                               (rid,)).fetchone()[0] == '失败'


def test_tampered_fit_coefficients_break_verification(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'bump', bumpy_mesh_points(), root=tmp_path)
    rid = engine.run(
        db, raster,
        contract(survey_id='bump', correction_model='global_affine',
                 control_residual_limit=1.0),
        tmp_path / 'out.tif')
    with connect(db) as conn:
        row = conn.execute('SELECT coef FROM control_fit WHERE run_id=?',
                           (rid,)).fetchone()
        coef = json.loads(row['coef'])
        coef['coefficients']['cx'] += 0.7
        conn.execute('UPDATE control_fit SET coef=? WHERE run_id=?',
                     (json.dumps(coef), rid))
    assert 'control_fit_tampered' in engine.audit(db, rid)


def test_tampered_cell_basis_breaks_verification(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'bump', bumpy_mesh_points(), root=tmp_path)
    rid = engine.run(
        db, raster,
        contract(survey_id='bump', correction_model='local_mesh',
                 control_residual_limit=1.0),
        tmp_path / 'out.tif')
    with connect(db) as conn:
        conn.execute('UPDATE cell_basis SET source_col=source_col+0.4 WHERE run_id=?',
                     (rid,))
    assert 'cell_basis_mismatch' in engine.audit(db, rid)
    assert connect(db).execute('SELECT status FROM run WHERE id=?',
                               (rid,)).fetchone()[0] == '失败'


def test_tampered_fit_residuals_break_verification(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'bump', bumpy_mesh_points(), root=tmp_path)
    rid = engine.run(
        db, raster,
        contract(survey_id='bump', correction_model='global_affine',
                 control_residual_limit=1.0),
        tmp_path / 'out.tif')
    with connect(db) as conn:
        conn.execute('UPDATE control_fit_point SET residual=residual+1.0 WHERE fit_id='
                     '(SELECT id FROM control_fit WHERE run_id=?)', (rid,))
    assert 'control_fit_tampered' in engine.audit(db, rid)


def test_cell_and_basis_divergence_breaks_verification(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'bump', bumpy_mesh_points(), root=tmp_path)
    rid = engine.run(
        db, raster,
        contract(survey_id='bump', correction_model='local_mesh',
                 control_residual_limit=1.0),
        tmp_path / 'out.tif')
    # Rewriting only the cell coordinate leaves the basis internally consistent
    # but no longer in agreement with the re-derived inverse location.
    with connect(db) as conn:
        conn.execute('UPDATE cell SET source_col=source_col+0.3 WHERE run_id=?',
                     (rid,))
    assert 'cell_basis_mismatch' in engine.audit(db, rid)


def test_report_contains_fit_residuals_and_cell_basis(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'bump', bumpy_mesh_points(), root=tmp_path)
    rid = engine.run(
        db, raster,
        contract(survey_id='bump', correction_model='global_affine',
                 control_residual_limit=1.0),
        tmp_path / 'out.tif')
    obj = json.loads(engine.report(db, rid, tmp_path / 'r.json').read_text())
    assert obj['control']['fit']['model'] == 'global_affine'
    assert len(obj['control']['points']) == 9
    assert all('residual' in p for p in obj['control']['points'])
    assert len(obj['control']['basis']) == 36
    assert all('source_row' in b for b in obj['control']['basis'])
    md = (tmp_path / 'r.md').read_text()
    assert 'global_affine' in md


def _survey_points(db, survey_id):
    return [{'id': p['point_id'], 'source_row': p['source_row'],
             'source_col': p['source_col'], 'target_x': p['target_x'],
             'target_y': p['target_y'], 'weight': p['weight']}
            for p in control.inspect_survey(db, survey_id)['points']]


def test_weighted_affine_downweights_gross_outlier(tmp_path):
    # A grossly wrong point with a near-zero weight must not move the fit.
    # With full weight the same point is rejected by the residual gate.
    pts = corner_shift_points(0.0, n=4)
    pts.append({'id': 'OUT', 'source_row': 1.5, 'source_col': 1.5,
                'target_x': 9.0, 'target_y': 9.0, 'weight': 1e-9})
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(4.0), (4, 1)))
    db = tmp_path / 'ledger.db'
    register_survey(db, raster, 'weighted', pts, root=tmp_path)
    # The near-zero weight keeps the fit identity-like even though the loose
    # residual gate has to make room for the outlier's ~9.9 target-unit miss.
    model, records = geometry.fit_global_affine(_survey_points(db, 'weighted'), 10.0)
    assert model.ax == pytest.approx(1.0, abs=1e-4)
    assert abs(model.cx) < 1e-3
    heavy = [dict(p, weight=1.0) for p in pts]
    register_survey(db, raster, 'heavy', heavy, root=tmp_path)
    # Full weight on the same outlier both distorts the fit and trips the gate.
    heavy_model, _ = geometry.fit_global_affine(_survey_points(db, 'heavy'), 10.0)
    assert abs(heavy_model.ax - 1.0) > 0.1
    with pytest.raises(geometry.GeometryError, match='residual_limit_exceeded'):
        geometry.fit_global_affine(_survey_points(db, 'heavy'), 1.0)
    # And the weighted survey runs cleanly end to end.
    rid = engine.run(
        db, raster,
        contract(bounds=[0, 0, 4, 4], width=4, height=4,
                 survey_id='weighted', correction_model='global_affine',
                 control_residual_limit=10.0),
        tmp_path / 'out.tif')
    assert engine.audit(db, rid) == []


def test_cli_runs_corrected_workflow(tmp_path):
    raster = tmp_path / 'src.tif'
    write_raster(raster, np.tile(np.arange(6.0), (6, 1)))
    obs = tmp_path / 'obs.json'
    obs.write_text(json.dumps({'target_crs': 'EPSG:4326',
                               'points': bumpy_mesh_points()}))
    db = tmp_path / 'ledger.db'
    contract_path = tmp_path / 'contract.json'
    contract_path.write_text(json.dumps(
        contract(bounds=[3.5, 1.5, 4.5, 2.5], width=1, height=1)))
    out = tmp_path / 'cli-out.tif'

    def cli(*args):
        return subprocess.run(
            [sys.executable, '-B', '-m', 'gridreproject', *map(str, args)],
            cwd=Path(__file__).resolve().parent.parent, capture_output=True,
            text=True, check=True)

    cli('control-register', '--db', db, '--id', 'bump', '--source', raster,
        '--observations', obs)
    proc = cli('run', '--db', db, '--source', raster, '--contract',
               contract_path, '--out', out, '--survey-id', 'bump',
               '--correction-model', 'local_mesh',
               '--control-residual-limit', '1.0')
    rid = int(proc.stdout.strip())
    assert json.loads(cli('audit', '--db', db, '--run-id', rid).stdout) == []
    report = tmp_path / 'report.json'
    cli('report', '--db', db, '--run-id', rid, '--path', report)
    body = json.loads(report.read_text())
    assert body['control']['fit']['model'] == 'local_mesh'
    with rasterio.open(out) as src:
        assert src.read(1)[0, 0] == 3.0
