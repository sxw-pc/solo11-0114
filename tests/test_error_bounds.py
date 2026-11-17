import json

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from gridreproject import engine, operations
from gridreproject.store import connect

TRANSFORM = from_bounds(0, 0, 2, 2, 2, 2)
BASE = {
    'crs': 'EPSG:4326', 'bounds': [0, 0, 2, 2], 'width': 2, 'height': 2,
    'kind': 'continuous', 'method': 'bilinear', 'min_coverage': 1.0,
}


def raster(path, data, transform=TRANSFORM, nodata=-9999, **profile):
    opts = dict(driver='GTiff', width=2, height=2, count=1, dtype='float64',
                crs='EPSG:4326', transform=transform, nodata=nodata)
    opts.update(profile)
    with rasterio.open(path, 'w', **opts) as dst:
        dst.write(np.array(data, dtype='float64'), 1)
    return path


def contract(**changes):
    obj = json.loads(json.dumps(BASE))
    obj.update(changes)
    return obj


def uniform(bound, version='m1'):
    return {'version': version, 'abs_bound': bound}


def source(tmp_path, data=((1., 2.), (3., 4.))):
    return raster(tmp_path / 'in.tif', data)


def shifted(**changes):
    obj = contract(bounds=[0.5, 0.5, 2.5, 2.5], min_coverage=0.0)
    obj.update(changes)
    return obj


def test_uniform_error_bounds_for_bilinear_interpolation(tmp_path):
    rid = engine.run(tmp_path / 'db', source(tmp_path),
                     contract(error_model=uniform(0.5)), tmp_path / 'out.tif')
    assert engine.audit(tmp_path / 'db', rid) == []
    db = connect(tmp_path / 'db')
    rows = db.execute('SELECT row,col,value,value_lower,value_upper,error_known '
                      'FROM cell WHERE run_id=? ORDER BY row,col', (rid,)).fetchall()
    assert [r['value'] for r in rows] == [1.0, 2.0, 3.0, 4.0]
    assert [r['value_lower'] for r in rows] == [0.5, 1.5, 2.5, 3.5]
    assert [r['value_upper'] for r in rows] == [1.5, 2.5, 3.5, 4.5]
    assert all(r['error_known'] == 1 for r in rows)


def test_bilinear_bounds_use_only_neighbours_taking_part_in_normalisation(tmp_path):
    # A shifted target grid pulls a fraction of the lower-left source cell.
    rid = engine.run(tmp_path / 'db', source(tmp_path),
                     shifted(error_model=uniform(0.4)), tmp_path / 'out.tif')
    assert engine.audit(tmp_path / 'db', rid) == []
    db = connect(tmp_path / 'db')
    cell = db.execute('SELECT * FROM cell WHERE run_id=? AND row=0 AND col=0', (rid,)).fetchone()
    evidence = json.loads(cell['weight_evidence'])
    assert evidence['mode'] == 'normalised_bilinear'
    assert 0 < evidence['weight_sum'] <= 1
    # Bound is normalised by the same valid weight that produced the value.
    assert cell['value_lower'] == pytest.approx(cell['value'] - evidence['bound'])
    assert cell['value_upper'] == pytest.approx(cell['value'] + evidence['bound'])
    assert evidence['bound'] == pytest.approx(0.4)


def test_bilinear_unknown_error_only_matters_when_neighbour_is_weighted(tmp_path):
    three = from_bounds(0, 0, 3, 3, 3, 3)
    grid = raster(tmp_path / 'err.tif', [[0.1, 0.1, 0.1], [0.1, 0.1, 0.1],
                                         [0.1, 0.1, -9999]],
                  transform=three, width=3, height=3)
    src = raster(tmp_path / 'src.tif', [[1., 2., 3.], [4., 5., 6.], [7., 8., 9.]],
                 transform=three, width=3, height=3)
    cfg = contract(bounds=[1, 1, 2, 2], width=1, height=1,
                   error_model={'version': 'v', 'error_grid': str(grid)})
    rid = engine.run(tmp_path / 'db', src, cfg, tmp_path / 'out.tif')
    cell = connect(tmp_path / 'db').execute(
        'SELECT * FROM cell WHERE run_id=?', (rid,)).fetchone()
    # The NoData error pixel is outside this target cell's bilinear stencil.
    assert cell['value'] == pytest.approx(5.0)
    assert cell['error_known'] == 1
    assert cell['value_lower'] == pytest.approx(4.9)
    assert cell['value_upper'] == pytest.approx(5.1)

    # Move the stencil so the unknown neighbour is actually weighted.
    cfg['bounds'] = [1.5, 0.5, 2.5, 1.5]
    rid = engine.run(tmp_path / 'db', src, cfg, tmp_path / 'out2.tif')
    cell = connect(tmp_path / 'db').execute(
        'SELECT * FROM cell WHERE run_id=?', (rid,)).fetchone()
    assert cell['value'] == pytest.approx(7.0)
    assert cell['error_known'] == 0
    assert cell['value_lower'] is None and cell['value_upper'] is None
    assert json.loads(cell['weight_evidence'])['unknown_weight_sum'] == pytest.approx(0.25)


def test_area_weights_accumulate_error_bound(tmp_path):
    same = engine.run(tmp_path / 'db', source(tmp_path),
                      contract(kind='extensive', method='area', error_model=uniform(0.5)),
                      tmp_path / 'same.tif')
    assert engine.audit(tmp_path / 'db', same) == []
    db = connect(tmp_path / 'db')
    row = db.execute('SELECT * FROM cell WHERE run_id=? AND row=0 AND col=0', (same,)).fetchone()
    # Same-aligned area resampling returns the single source cell amount.
    assert (row['value'], row['value_lower'], row['value_upper']) == (1.0, 0.5, 1.5)
    assert json.loads(row['weight_evidence'])['mode'] == 'area'

    down = engine.run(tmp_path / 'db', source(tmp_path),
                      contract(kind='extensive', method='area', width=1, height=1,
                               error_model=uniform(0.5)), tmp_path / 'down.tif')
    assert engine.audit(tmp_path / 'db', down) == []
    row = connect(tmp_path / 'db').execute(
        'SELECT * FROM cell WHERE run_id=? AND row=0 AND col=0', (down,)).fetchone()
    # Four source pixels, each with 0.5 absolute error, summed by area weight.
    assert (row['value'], row['value_lower'], row['value_upper']) == (10.0, 8.0, 12.0)


def test_error_grid_nodata_means_unknown_and_blocks_decision(tmp_path):
    grid = raster(tmp_path / 'err.tif', [[0.1, 0.2], [0.3, -9999]])
    rid = engine.run(tmp_path / 'db', source(tmp_path),
                     contract(error_model={'version': 'local', 'error_grid': str(grid)}),
                     tmp_path / 'out.tif')
    db = connect(tmp_path / 'db')
    missing = db.execute('SELECT * FROM cell WHERE run_id=? AND row=1 AND col=1', (rid,)).fetchone()
    assert missing['value'] == 4.0          # nominal value still known
    assert missing['value_lower'] is None and missing['value_upper'] is None
    assert missing['error_known'] == 0
    assert db.execute("SELECT count(*) FROM issue WHERE run_id=? AND code='error_unknown'",
                      (rid,)).fetchone()[0] == 1

    operations.register_zone(tmp_path / 'db', 'z', [[0, 0], [1, 1]], 1.0, 10.0,
                             strategy='mean_bound', strategy_version='s1')
    result = operations.evaluate_zone(tmp_path / 'db', rid, 'z')
    assert result['status'] == '证据不足'
    assert operations.decide_release(tmp_path / 'db', 'r', rid, ['z'])['status'] == '暂缓'


def test_error_grid_nodata_is_never_zero_even_when_nominal_is_safe(tmp_path):
    grid = raster(tmp_path / 'err.tif', [[-9999, 0.1], [0.1, 0.1]])
    rid = engine.run(tmp_path / 'db', source(tmp_path, [[1., 1.], [1., 1.]]),
                     contract(error_model={'version': 'local', 'error_grid': str(grid)}),
                     tmp_path / 'out.tif')
    operations.register_zone(tmp_path / 'db', 'z', [[0, 0]], 1.0, 100.0,
                             strategy='hotspot_bound', strategy_version='s1')
    assert operations.evaluate_zone(tmp_path / 'db', rid, 'z')['status'] == '证据不足'


def test_category_rejects_numeric_error_model(tmp_path):
    with pytest.raises(ValueError, match='error_model_kind_conflict'):
        engine.run(tmp_path / 'db', source(tmp_path),
                   contract(kind='category', method='nearest', error_model=uniform(0.1)),
                   tmp_path / 'out.tif')
    with pytest.raises(ValueError, match='bad_error_model'):
        engine.run(tmp_path / 'db', source(tmp_path),
                   contract(error_model={'version': 'v', 'abs_bound': -1}), tmp_path / 'a.tif')
    with pytest.raises(ValueError, match='bad_error_model'):
        engine.run(tmp_path / 'db', source(tmp_path),
                   contract(error_model={'version': 'v', 'abs_bound': 1,
                                         'error_grid': 'x.tif'}), tmp_path / 'b.tif')


def test_error_grid_must_share_source_grid(tmp_path):
    other = raster(tmp_path / 'err.tif', [[0.1, 0.1], [0.1, 0.1]],
                   transform=from_bounds(0, 0, 4, 4, 2, 2))
    with pytest.raises(ValueError, match='error_grid_mismatch'):
        engine.run(tmp_path / 'db', source(tmp_path),
                   contract(error_model={'version': 'v', 'error_grid': str(other)}),
                   tmp_path / 'out.tif')


def test_zone_strategies_disagree_on_a_hotspot(tmp_path):
    # Two cells, intervals [1,2] and [8,9], limit 6.
    src = source(tmp_path, [[1.5, 8.5], [1.5, 8.5]])
    cfg = contract(error_model=uniform(0.5))
    rid = engine.run(tmp_path / 'db', src, cfg, tmp_path / 'out.tif')
    operations.register_zone(tmp_path / 'db', 'mean', [[0, 0], [0, 1]], 1.0, 6.0,
                             strategy='mean_bound', strategy_version='s1')
    operations.register_zone(tmp_path / 'db', 'hot', [[0, 0], [0, 1]], 1.0, 6.0,
                             strategy='hotspot_bound', strategy_version='s1')
    mean = operations.evaluate_zone(tmp_path / 'db', rid, 'mean')
    hot = operations.evaluate_zone(tmp_path / 'db', rid, 'hot')
    assert (mean['lower'], mean['upper']) == pytest.approx((4.5, 5.5))
    assert mean['status'] == '正常'
    assert (hot['lower'], hot['upper']) == pytest.approx((8.0, 9.0))
    assert hot['status'] == '超限'
    release_mean = operations.decide_release(tmp_path / 'db', 'mean-release', rid, ['mean'])
    release_hot = operations.decide_release(tmp_path / 'db', 'hot-release', rid, ['hot'])
    assert release_mean['status'] == '准予发布'
    assert release_hot['status'] == '拒发'
    db = connect(tmp_path / 'db')
    frozen = db.execute("SELECT strategy,strategy_version,lower_bound,upper_bound "
                        "FROM zone_decision WHERE run_id=? AND zone_id='hot'", (rid,)).fetchone()
    assert tuple(frozen) == ('hotspot_bound', 's1', 8.0, 9.0)


def test_interval_crossing_limit_is_insufficient_evidence(tmp_path):
    # Nominal 6 with +/- 2 spans the threshold, so neither safe nor exceeded.
    rid = engine.run(tmp_path / 'db', source(tmp_path, [[6., 6.], [6., 6.]]),
                     contract(error_model=uniform(2.0)), tmp_path / 'out.tif')
    for strategy in ('mean_bound', 'hotspot_bound'):
        name = f'z-{strategy}'
        operations.register_zone(tmp_path / 'db', name, [[0, 0]], 1.0, 6.0,
                                 strategy=strategy, strategy_version='s1')
        result = operations.evaluate_zone(tmp_path / 'db', rid, name)
        assert result['status'] == '证据不足'
        assert result['lower'] == 4.0 and result['upper'] == 8.0
        assert operations.decide_release(tmp_path / 'db', f'r-{strategy}', rid,
                                         [name])['status'] == '暂缓'


def test_hotspot_upper_on_limit_is_safe_lower_above_limit_is_exceeded(tmp_path):
    rid = engine.run(tmp_path / 'db', source(tmp_path, [[5., 5.], [5., 5.]]),
                     contract(error_model=uniform(1.0)), tmp_path / 'out.tif')
    operations.register_zone(tmp_path / 'db', 'edge', [[0, 0]], 1.0, 6.0,
                             strategy='hotspot_bound', strategy_version='s1')
    assert operations.evaluate_zone(tmp_path / 'db', rid, 'edge')['status'] == '正常'
    operations.register_zone(tmp_path / 'db', 'over', [[0, 0]], 1.0, 3.0,
                             strategy='hotspot_bound', strategy_version='s1')
    assert operations.evaluate_zone(tmp_path / 'db', rid, 'over')['status'] == '超限'


def test_strategy_is_frozen_and_immutable(tmp_path):
    db = tmp_path / 'db'
    rid = engine.run(db, source(tmp_path), contract(error_model=uniform(0.1)),
                     tmp_path / 'out.tif')
    assert rid
    operations.register_zone(db, 'z', [[0, 0]], 1.0, 6.0,
                             strategy='mean_bound', strategy_version='s1')
    with pytest.raises(ValueError, match='zone_version_conflict'):
        operations.register_zone(db, 'z', [[0, 0]], 1.0, 6.0,
                                 strategy='hotspot_bound', strategy_version='s1')
    with pytest.raises(ValueError, match='zone_version_conflict'):
        operations.register_zone(db, 'z', [[0, 0]], 1.0, 6.0,
                                 strategy='mean_bound', strategy_version='s2')
    with pytest.raises(ValueError, match='bad_zone_strategy'):
        operations.register_zone(db, 'w', [[0, 0]], 1.0, 6.0,
                                 strategy='mean_bound', strategy_version=None)
    with pytest.raises(ValueError, match='bad_zone_strategy'):
        operations.register_zone(db, 'v', [[0, 0]], 1.0, 6.0,
                                 strategy='average_bound', strategy_version='s1')


def test_interval_strategy_needs_a_declared_error_model(tmp_path):
    rid = engine.run(tmp_path / 'db', source(tmp_path), contract(), tmp_path / 'out.tif')
    operations.register_zone(tmp_path / 'db', 'z', [[0, 0]], 1.0, 6.0,
                             strategy='mean_bound', strategy_version='s1')
    assert operations.evaluate_zone(tmp_path / 'db', rid, 'z')['status'] == '证据不足'


def test_tampered_error_grid_invalidates_audit_and_release(tmp_path):
    grid = raster(tmp_path / 'err.tif', [[0.1, 0.1], [0.1, 0.1]])
    rid = engine.run(tmp_path / 'db', source(tmp_path),
                     contract(error_model={'version': 'local', 'error_grid': str(grid)}),
                     tmp_path / 'out.tif')
    operations.register_zone(tmp_path / 'db', 'z', [[0, 0], [0, 1]], 1.0, 10.0,
                             strategy='mean_bound', strategy_version='s1')
    assert operations.evaluate_zone(tmp_path / 'db', rid, 'z')['status'] == '正常'
    assert operations.decide_release(tmp_path / 'db', 'rel', rid, ['z'])['status'] == '准予发布'

    raster(tmp_path / 'err.tif', [[5.0, 5.0], [5.0, 5.0]])
    assert 'error_hash' in engine.audit(tmp_path / 'db', rid)
    db = connect(tmp_path / 'db')
    assert db.execute('SELECT status FROM run WHERE id=?', (rid,)).fetchone()[0] == '失败'
    assert db.execute("SELECT status FROM release_request WHERE id='rel'").fetchone()[0] == '暂缓'


def test_tampered_persisted_interval_is_detected(tmp_path):
    rid = engine.run(tmp_path / 'db', source(tmp_path), contract(error_model=uniform(0.5)),
                     tmp_path / 'out.tif')
    db = connect(tmp_path / 'db')
    with db:
        db.execute('UPDATE cell SET value_upper=? WHERE run_id=? AND row=0 AND col=0',
                   (99.0, rid))
    assert 'interval_mismatch' in engine.audit(tmp_path / 'db', rid)
    assert connect(tmp_path / 'db').execute(
        'SELECT status FROM run WHERE id=?', (rid,)).fetchone()[0] == '失败'


def test_removing_persisted_interval_is_detected(tmp_path):
    rid = engine.run(tmp_path / 'db', source(tmp_path), contract(error_model=uniform(0.5)),
                     tmp_path / 'out.tif')
    db = connect(tmp_path / 'db')
    with db:
        db.execute('UPDATE cell SET value_lower=NULL,error_known=0 '
                   'WHERE run_id=? AND row=0 AND col=0', (rid,))
    assert 'interval_mismatch' in engine.audit(tmp_path / 'db', rid)


def test_missing_error_grid_fails_audit(tmp_path):
    grid = raster(tmp_path / 'err.tif', [[0.1, 0.1], [0.1, 0.1]])
    rid = engine.run(tmp_path / 'db', source(tmp_path),
                     contract(error_model={'version': 'local', 'error_grid': str(grid)}),
                     tmp_path / 'out.tif')
    (tmp_path / 'err.tif').unlink()
    assert 'error_grid' in engine.audit(tmp_path / 'db', rid)


def test_release_request_cannot_be_rewritten_under_new_policy(tmp_path):
    rid = engine.run(tmp_path / 'db', source(tmp_path), contract(error_model=uniform(1.0)),
                     tmp_path / 'out.tif')
    operations.register_zone(tmp_path / 'db', 'z', [[0, 0]], 1.0, 1.0,
                             strategy='mean_bound', strategy_version='s1')
    decisions = operations.evaluate_zone(tmp_path / 'db', rid, 'z')
    assert decisions['status'] == '证据不足'      # [0,2] straddles limit 1.0
    assert operations.decide_release(tmp_path / 'db', 'rel', rid, ['z'])['status'] == '暂缓'
    # Switching the same zone name to the legacy nominal policy must not be
    # able to rewrite the frozen release request.
    with pytest.raises(ValueError, match='zone_version_conflict'):
        operations.register_zone(tmp_path / 'db', 'z', [[0, 0]], 1.0, 1.0)
    operations.register_zone(tmp_path / 'db', 'legacy', [[0, 0]], 1.0, 1.0)
    operations.evaluate_zone(tmp_path / 'db', rid, 'legacy')
    with pytest.raises(ValueError, match='release_identity_conflict'):
        operations.decide_release(tmp_path / 'db', 'rel', rid, ['legacy'])
    # A fresh request id is the only way to record the new policy.
    fresh = operations.decide_release(tmp_path / 'db', 'rel2', rid, ['legacy'])
    assert fresh['status'] == '暂缓' and fresh['reason'] == 'evidence_gap'


def test_release_records_frozen_policy_and_bounds(tmp_path):
    rid = engine.run(tmp_path / 'db', source(tmp_path), contract(error_model=uniform(0.5)),
                     tmp_path / 'out.tif')
    operations.register_zone(tmp_path / 'db', 'z', [[0, 0], [0, 1]], 1.0, 10.0,
                             strategy='mean_bound', strategy_version='s1')
    operations.evaluate_zone(tmp_path / 'db', rid, 'z')
    result = operations.decide_release(tmp_path / 'db', 'rel', rid, ['z'])
    assert result['status'] == '准予发布'
    db = connect(tmp_path / 'db')
    row = db.execute("SELECT * FROM release_request WHERE id='rel'").fetchone()
    policy = json.loads(row['policy'])
    assert policy['model_version'] == 'm1'
    assert policy['zones'][0]['strategy'] == 'mean_bound'
    assert policy['zones'][0]['strategy_version'] == 's1'
    assert (row['lower_bound'], row['upper_bound']) == pytest.approx((1.0, 2.0))
    # The same policy can be re-decided idempotently.
    again = operations.decide_release(tmp_path / 'db', 'rel', rid, ['z'])
    assert again['status'] == '准予发布'
    # But a different run under the same request id is a conflict.
    other = engine.run(tmp_path / 'db', source(tmp_path), contract(error_model=uniform(0.5)),
                       tmp_path / 'out2.tif')
    operations.register_zone(tmp_path / 'db', 'z', [[0, 0], [0, 1]], 1.0, 10.0,
                             strategy='mean_bound', strategy_version='s1')
    operations.evaluate_zone(tmp_path / 'db', other, 'z')
    with pytest.raises(ValueError, match='release_identity_conflict'):
        operations.decide_release(tmp_path / 'db', 'rel', other, ['z'])


def test_tampered_zone_bounds_invalidate_run_and_release(tmp_path):
    rid = engine.run(tmp_path / 'db', source(tmp_path), contract(error_model=uniform(0.5)),
                     tmp_path / 'out.tif')
    operations.register_zone(tmp_path / 'db', 'z', [[0, 0], [0, 1]], 1.0, 10.0,
                             strategy='mean_bound', strategy_version='s1')
    operations.evaluate_zone(tmp_path / 'db', rid, 'z')
    assert operations.decide_release(tmp_path / 'db', 'rel', rid, ['z'])['status'] == '准予发布'
    db = connect(tmp_path / 'db')
    with db:
        db.execute("UPDATE zone_decision SET lower_bound=lower_bound-1,upper_bound=upper_bound+1 "
                   "WHERE run_id=? AND zone_id='z'", (rid,))
    assert 'zone_evidence_mismatch' in engine.audit(tmp_path / 'db', rid)
    db = connect(tmp_path / 'db')
    assert db.execute('SELECT status FROM run WHERE id=?', (rid,)).fetchone()[0] == '失败'
    assert db.execute("SELECT status FROM release_request WHERE id='rel'").fetchone()[0] == '暂缓'


def test_tampered_release_policy_is_detected(tmp_path):
    rid = engine.run(tmp_path / 'db', source(tmp_path), contract(error_model=uniform(0.5)),
                     tmp_path / 'out.tif')
    operations.register_zone(tmp_path / 'db', 'z', [[0, 0]], 1.0, 10.0,
                             strategy='mean_bound', strategy_version='s1')
    operations.evaluate_zone(tmp_path / 'db', rid, 'z')
    operations.decide_release(tmp_path / 'db', 'rel', rid, ['z'])
    db = connect(tmp_path / 'db')
    policy = json.loads(db.execute("SELECT policy FROM release_request WHERE id='rel'")
                        .fetchone()[0])
    policy['zones'][0]['strategy_version'] = 'forged'
    with db:
        db.execute("UPDATE release_request SET policy=? WHERE id='rel'", (json.dumps(policy),))
    assert 'zone_evidence_mismatch' in engine.audit(tmp_path / 'db', rid)


def test_tampered_model_version_is_detected(tmp_path):
    rid = engine.run(tmp_path / 'db', source(tmp_path), contract(error_model=uniform(0.5)),
                     tmp_path / 'out.tif')
    db = connect(tmp_path / 'db')
    with db:
        db.execute("UPDATE run SET error_model_version='forged' WHERE id=?", (rid,))
    assert 'error_model' in engine.audit(tmp_path / 'db', rid)
    assert connect(tmp_path / 'db').execute(
        'SELECT status FROM run WHERE id=?', (rid,)).fetchone()[0] == '失败'


def test_legacy_release_cannot_be_upgraded_to_an_interval_policy(tmp_path):
    rid = engine.run(tmp_path / 'db', source(tmp_path), contract(), tmp_path / 'out.tif')
    operations.register_zone(tmp_path / 'db', 'z', [[1, 0], [1, 1]], 1.0, 10.0)
    assert operations.evaluate_zone(tmp_path / 'db', rid, 'z')['status'] == '正常'
    assert operations.decide_release(tmp_path / 'db', 'rel', rid, ['z'])['status'] == '准予发布'
    # Register a same-scope interval zone on an error-aware run, then try to
    # reuse the legacy request id: this is a policy change, not a re-decision.
    aware = engine.run(tmp_path / 'db', source(tmp_path), contract(error_model=uniform(0.1)),
                       tmp_path / 'aware.tif')
    operations.register_zone(tmp_path / 'db', 'z2', [[1, 0], [1, 1]], 1.0, 10.0,
                             strategy='mean_bound', strategy_version='s1')
    db = connect(tmp_path / 'db')
    with db:
        db.execute("UPDATE release_request SET zone_ids=?,run_id=? WHERE id='rel'",
                   (json.dumps(['z2']), aware))
    operations.evaluate_zone(tmp_path / 'db', aware, 'z2')
    with pytest.raises(ValueError, match='release_identity_conflict'):
        operations.decide_release(tmp_path / 'db', 'rel', aware, ['z2'])


def test_runs_without_error_model_keep_nominal_behaviour(tmp_path):
    rid = engine.run(tmp_path / 'db', source(tmp_path), contract(), tmp_path / 'out.tif')
    db = connect(tmp_path / 'db')
    row = db.execute('SELECT * FROM cell WHERE run_id=? AND row=0 AND col=0', (rid,)).fetchone()
    assert row['value_lower'] is None and row['value_upper'] is None
    assert row['error_known'] is None
    assert db.execute('SELECT error_model FROM run WHERE id=?', (rid,)).fetchone()[0] is None
    operations.register_zone(tmp_path / 'db', 'north', [[0, 0], [0, 1]], 1.0, 1.0)
    assert operations.evaluate_zone(tmp_path / 'db', rid, 'north')['status'] == '超限'
    operations.register_zone(tmp_path / 'db', 'south', [[1, 0], [1, 1]], 1.0, 10.0)
    assert operations.evaluate_zone(tmp_path / 'db', rid, 'south')['status'] == '正常'
    assert operations.decide_release(tmp_path / 'db', 'old', rid, ['south'])['status'] == '准予发布'


def test_old_ledger_schema_is_upgraded_in_place(tmp_path):
    import sqlite3
    path = tmp_path / 'legacy.db'
    raw = sqlite3.connect(path)
    raw.executescript("""
    CREATE TABLE source(id INTEGER PRIMARY KEY,path TEXT NOT NULL,hash TEXT NOT NULL UNIQUE,width INT,height INT,crs TEXT,nodata REAL,unit TEXT);
    CREATE TABLE contract(id INTEGER PRIMARY KEY,hash TEXT NOT NULL UNIQUE,body TEXT NOT NULL);
    CREATE TABLE run(id INTEGER PRIMARY KEY,source_id INT,contract_id INT,status TEXT NOT NULL,method TEXT NOT NULL,started TEXT,ended TEXT);
    CREATE TABLE cell(run_id INT,row INT,col INT,value REAL,coverage REAL,source_row REAL,source_col REAL,weight REAL,PRIMARY KEY(run_id,row,col));
    CREATE TABLE issue(id INTEGER PRIMARY KEY,run_id INT,row INT,col INT,code TEXT NOT NULL,detail TEXT);
    CREATE TABLE output(run_id INT PRIMARY KEY,path TEXT NOT NULL,hash TEXT NOT NULL);
    CREATE TABLE zone(id TEXT PRIMARY KEY,cell_ids TEXT NOT NULL,min_coverage REAL NOT NULL,limit_value REAL NOT NULL);
    CREATE TABLE zone_decision(run_id INT,zone_id TEXT,status TEXT NOT NULL,measured REAL,coverage REAL NOT NULL,evidence TEXT NOT NULL,PRIMARY KEY(run_id,zone_id));
    CREATE TABLE release_request(id TEXT PRIMARY KEY,run_id INT,zone_ids TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,created TEXT);
    """)
    raw.commit()
    raw.close()
    # An old run is still readable with its original meaning.
    rid = engine.run(path, source(tmp_path), contract(), tmp_path / 'out.tif')
    assert engine.audit(path, rid) == []
    db = connect(path)
    assert db.execute('SELECT error_model FROM run WHERE id=?', (rid,)).fetchone()[0] is None
    operations.register_zone(path, 'north', [[0, 0], [0, 1]], 1.0, 1.0)
    assert operations.evaluate_zone(path, rid, 'north')['status'] == '超限'
    assert operations.decide_release(path, 'legacy', rid, ['north'])['status'] == '拒发'


def test_report_has_crossing_and_missing_error_evidence(tmp_path):
    grid = raster(tmp_path / 'err.tif', [[3.0, 0.1], [0.1, -9999]])
    rid = engine.run(tmp_path / 'db', source(tmp_path, [[6., 1.], [1., 1.]]),
                     contract(error_model={'version': 'local', 'error_grid': str(grid)}),
                     tmp_path / 'out.tif')
    operations.register_zone(tmp_path / 'db', 'z', [[0, 0], [0, 1], [1, 1]], 1.0, 6.0,
                             strategy='mean_bound', strategy_version='s1')
    operations.evaluate_zone(tmp_path / 'db', rid, 'z')
    path = engine.report(tmp_path / 'db', rid, tmp_path / 'report.json')
    obj = json.loads(path.read_text(encoding='utf-8'))
    flagged = {(c['row'], c['col']): c for c in obj['zones'][0]['flagged_cells']}
    assert flagged[(0, 0)]['crosses_limit'] is True
    assert flagged[(1, 1)]['missing_error'] is True
    assert (0, 1) not in flagged
    md = path.with_suffix('.md').read_text(encoding='utf-8')
    assert '跨阈值' in md and '误差未知' in md
    assert 'lng' not in md and 'lat' not in md
