import json
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds
from gridreproject import engine, corridor
from gridreproject.store import connect


def test_basic_corridor_and_frozen_groups(tmp_path):
    source = tmp_path / 'cost.tif'
    with rasterio.open(source, 'w', driver='GTiff', width=3, height=3, count=1,
                       dtype='float64', crs='EPSG:4326',
                       transform=from_bounds(0, 0, 3, 3, 3, 3), nodata=-9999) as dst:
        dst.write(np.ones((3, 3), dtype='float64'), 1)
    contract = {'crs': 'EPSG:4326', 'bounds': [0,0,3,3], 'width': 3, 'height': 3,
                'kind': 'continuous', 'method': 'bilinear', 'min_coverage': 1.0}
    db = tmp_path / 'ledger.db'
    rid = engine.run(db, source, contract, tmp_path / 'cost-out.tif')
    assert corridor.register(db, 'route-1', rid, [0,0], [2,2], 5,
                             [[[1,1]]]) == {'id': 'route-1'}
    result = corridor.search(db, 'route-1')
    assert result['status'] == '可通行' and result['cost'] == 4


# --- corridor_assessment fixtures -------------------------------------------
# Values: positive finite = known traversable cost, 0 = known obstacle,
# None = missing measurement (unknown cell, nodata in the source raster).

def make_grid_run(tmp_path, data, name='cost', db=None, kind='continuous',
                  method='bilinear'):
    h, w = len(data), len(data[0])
    src = tmp_path / f'{name}.tif'
    arr = np.array([[-9999.0 if v is None else float(v) for v in row] for row in data])
    with rasterio.open(src, 'w', driver='GTiff', width=w, height=h, count=1,
                       dtype='float64', crs='EPSG:4326',
                       transform=from_bounds(0, 0, w, h, w, h), nodata=-9999) as dst:
        dst.write(arr, 1)
    contract = {'crs': 'EPSG:4326', 'bounds': [0, 0, w, h], 'width': w, 'height': h,
                'kind': kind, 'method': method, 'min_coverage': 1.0}
    db = db or tmp_path / f'{name}.db'
    rid = engine.run(db, src, contract, tmp_path / f'{name}-out.tif')
    return db, rid


def corridor_5x5(tmp_path, bypass_unknown=False, name='cost', db=None):
    """One straight cost-1 corridor across the middle row; the bypass row is
    either certain obstacles or missing measurements."""
    data = [[0.0] * 5,
                [None] * 5 if bypass_unknown else [0.0] * 5,
                [1.0] * 5,
                [0.0] * 5,
                [0.0] * 5]
    return make_grid_run(tmp_path, data, name=name, db=db)


def test_single_fast_viable_and_search_semantics_preserved(tmp_path):
    db, rid = corridor_5x5(tmp_path)
    corridor.register(db, 'req-single', rid, [2, 0], [2, 4], 10)
    before = corridor.search(db, 'req-single')
    assert before['status'] == '可通行' and before['cost'] == 4
    result = corridor.corridor_assessment(db, 'req-single', 'single_fast')
    assert result['status'] == 'viable'
    assert result['reason'] == 'witnessed_in_known_graph'
    route = result['routes'][0]
    assert route['cells'] == [[2, 0], [2, 1], [2, 2], [2, 3], [2, 4]]
    assert route['cost'] == 4 and route['coverage'] == [1.0] * 5
    # The assessment must not rewrite the single-route corridor_result.
    row = connect(db).execute(
        'SELECT status,cost,cells FROM corridor_result WHERE request_id=?',
        ('req-single',)).fetchone()
    assert row['status'] == '可通行' and row['cost'] == 4
    assert json.loads(row['cells']) == [[2, 0], [2, 1], [2, 2], [2, 3], [2, 4]]


def test_survivable_pair_blocked_when_no_second_route(tmp_path):
    db, rid = corridor_5x5(tmp_path)
    corridor.register(db, 'req-pair', rid, [2, 0], [2, 4], 10, [[[0, 0], [4, 4]]])
    result = corridor.corridor_assessment(db, 'req-pair', 'survivable_pair')
    assert result['status'] == 'blocked'
    assert result['reason'] == 'infeasible_in_upper_bound'
    assert result['routes'] == []


def test_survivable_pair_indeterminate_with_missing_bypass(tmp_path):
    db, rid = corridor_5x5(tmp_path, bypass_unknown=True)
    corridor.register(db, 'req-pair-u', rid, [2, 0], [2, 4], 10)
    single = corridor.corridor_assessment(db, 'req-pair-u', 'single_fast')
    assert single['status'] == 'viable'  # the known corridor still suffices
    result = corridor.corridor_assessment(db, 'req-pair-u', 'survivable_pair')
    assert result['status'] == 'indeterminate'
    assert result['reason'] == 'unknown_cells_decisive'
    assert result['unknown_cells'] == [[1, 0], [1, 1], [1, 2], [1, 3], [1, 4]]
    assert result['routes'] == []  # the upper-bound guess is not a usable route


def test_survivable_pair_skips_cheap_pair_sharing_failure_group(tmp_path):
    # Three candidate routes: top (row 1, cost 8) and bottom (row 3, cost 8)
    # are disjoint but cross the same frozen failure group; only the pricier
    # middle route (row 2, cost 11) forms a survivable pair with either.
    # Running Dijkstra twice and keeping the two cheapest would misjudge.
    data = [[0.0] * 7,
            [1.0] * 7,
            [1.0, 2.0, 2.0, 2.0, 2.0, 2.0, 1.0],
            [1.0] * 7,
            [0.0] * 7]
    db, rid = make_grid_run(tmp_path, data)
    corridor.register(db, 'req-groups', rid, [2, 0], [2, 6], 11, [[[1, 3], [3, 3]]])
    result = corridor.corridor_assessment(db, 'req-groups', 'survivable_pair')
    assert result['status'] == 'viable'
    assert sorted(r['cost'] for r in result['routes']) == [8.0, 11.0]
    assert len(result['group_intersections']['0']) == 1
    first, second = ({tuple(c) for c in r['cells']} for r in result['routes'])
    assert first != second and first & second == {(2, 0), (2, 6)}
    assert any([2, 3] in r['cells'] for r in result['routes'])  # the pricier route


def test_unknown_cell_is_neither_shortcut_nor_obstacle(tmp_path):
    data = [[5.0, 5.0, 5.0],
            [1.0, None, 1.0],
            [0.0, 0.0, 0.0]]
    db, rid = make_grid_run(tmp_path, data)
    corridor.register(db, 'req-unknown', rid, [1, 0], [1, 2], 10)
    result = corridor.corridor_assessment(db, 'req-unknown', 'single_fast')
    # Known detour costs 16 > 10, but the unknown cell is not a free shortcut.
    assert result['status'] == 'indeterminate'
    assert result['unknown_cells'] == [[1, 1]]
    # Nor a certain obstacle: a budget the known detour meets flips to viable.
    corridor.register(db, 'req-unknown-2', rid, [1, 0], [1, 2], 20)
    ok = corridor.corridor_assessment(db, 'req-unknown-2', 'single_fast')
    assert ok['status'] == 'viable'
    assert ok['routes'][0]['cost'] == 16
    assert [1, 1] not in ok['routes'][0]['cells']


def test_cost_budget_and_path_legality(tmp_path):
    db, rid = corridor_5x5(tmp_path)
    corridor.register(db, 'req-budget', rid, [2, 0], [2, 4], 4)
    exact = corridor.corridor_assessment(db, 'req-budget', 'single_fast')
    assert exact['status'] == 'viable' and exact['routes'][0]['cost'] == 4
    cells = exact['routes'][0]['cells']
    assert cells[0] == [2, 0] and cells[-1] == [2, 4]
    for a, b in zip(cells, cells[1:]):
        assert abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1
    corridor.register(db, 'req-budget-2', rid, [2, 0], [2, 4], 3.5)
    short = corridor.corridor_assessment(db, 'req-budget-2', 'single_fast')
    assert short['status'] == 'blocked'
    assert short['evidence']['upper_min_cost'] == 4


def two_corridor_data(bottom_cost=1.0):
    return [[0.0] * 5,
            [1.0] * 5,
            [1.0, 0.0, 0.0, 0.0, 1.0],
            [bottom_cost] * 5,
            [0.0] * 5]


def test_pair_group_and_budget_negatives(tmp_path):
    db, rid = make_grid_run(tmp_path, two_corridor_data())
    # Both corridors exist but one frozen group crosses both -> no pair.
    corridor.register(db, 'req-g', rid, [2, 0], [2, 4], 10, [[[1, 2], [3, 2]]])
    assert corridor.corridor_assessment(db, 'req-g', 'survivable_pair')['status'] == 'blocked'
    # Same grid without the shared group -> viable pair.
    corridor.register(db, 'req-g2', rid, [2, 0], [2, 4], 10)
    ok = corridor.corridor_assessment(db, 'req-g2', 'survivable_pair')
    assert ok['status'] == 'viable' and len(ok['routes']) == 2
    # Second corridor too expensive for the budget -> pair blocked, single fine.
    db2, rid2 = make_grid_run(tmp_path, two_corridor_data(bottom_cost=3.0), name='dear')
    corridor.register(db2, 'req-g3', rid2, [2, 0], [2, 4], 10)
    assert corridor.corridor_assessment(db2, 'req-g3', 'survivable_pair')['status'] == 'blocked'
    assert corridor.corridor_assessment(db2, 'req-g3', 'single_fast')['status'] == 'viable'


def test_tamper_forces_unverifiable_and_preserves_old_results(tmp_path):
    db, rid = corridor_5x5(tmp_path)
    corridor.register(db, 'req-t', rid, [2, 0], [2, 4], 10)
    corridor.search(db, 'req-t')
    first = corridor.corridor_assessment(db, 'req-t', 'single_fast')
    assert first['status'] == 'viable'
    (tmp_path / 'cost-out.tif').write_bytes(b'tampered')
    second = corridor.corridor_assessment(db, 'req-t', 'single_fast')
    assert second['status'] == 'unverifiable' and second['reason'] == 'audit_failed'
    assert 'output_hash' in second['audit']
    # The stale viable verdict is not reused ...
    stored = connect(db).execute(
        "SELECT status FROM corridor_assessment WHERE request_id=? AND policy=?",
        ('req-t', 'single_fast')).fetchone()
    assert stored['status'] == 'unverifiable'
    # ... and the old single-route result row is untouched.
    row = connect(db).execute(
        'SELECT status,cost FROM corridor_result WHERE request_id=?', ('req-t',)).fetchone()
    assert row['status'] == '可通行' and row['cost'] == 4


def test_cell_evidence_tamper_changes_verdict(tmp_path):
    db, rid = corridor_5x5(tmp_path)
    corridor.register(db, 'req-c', rid, [2, 0], [2, 4], 10)
    assert corridor.corridor_assessment(db, 'req-c', 'single_fast')['status'] == 'viable'
    # Coverage tampering is not caught by the audit, but the frozen cell
    # digest changes, so the cell turns unknown and viable is withdrawn.
    with connect(db) as conn:
        conn.execute('UPDATE cell SET coverage=0 WHERE run_id=? AND row=2 AND col=3', (rid,))
    degraded = corridor.corridor_assessment(db, 'req-c', 'single_fast')
    assert degraded['status'] == 'indeterminate'
    assert degraded['unknown_cells'] == [[2, 3]]
    # Value tampering is caught by the audit itself.
    with connect(db) as conn:
        conn.execute('UPDATE cell SET value=9 WHERE run_id=? AND row=2 AND col=2', (rid,))
    forged = corridor.corridor_assessment(db, 'req-c', 'single_fast')
    assert forged['status'] == 'unverifiable'
    assert 'cell_mismatch' in forged['audit']


def test_assessment_idempotent_and_does_not_touch_corridor_result(tmp_path):
    db, rid = corridor_5x5(tmp_path)
    corridor.register(db, 'req-i', rid, [2, 0], [2, 4], 10)
    r1 = corridor.corridor_assessment(db, 'req-i', 'single_fast')
    r2 = corridor.corridor_assessment(db, 'req-i', 'single_fast')
    assert r1 == r2
    dbh = connect(db)
    assert dbh.execute('SELECT count(*) c FROM corridor_assessment '
                       'WHERE request_id=? AND policy=?', ('req-i', 'single_fast')).fetchone()['c'] == 1
    assert dbh.execute('SELECT count(*) c FROM corridor_result').fetchone()['c'] == 0


def test_rerun_does_not_inherit_old_conclusion(tmp_path):
    db, rid = corridor_5x5(tmp_path, bypass_unknown=True)
    corridor.register(db, 'req-r1', rid, [2, 0], [2, 4], 10)
    assert corridor.corridor_assessment(db, 'req-r1', 'survivable_pair')['status'] == 'indeterminate'
    # Re-measured world: the bypass is now known to be real obstacles.
    _, rid2 = corridor_5x5(tmp_path, name='remeasured', db=db)
    corridor.register(db, 'req-r2', rid2, [2, 0], [2, 4], 10)
    assert corridor.corridor_assessment(db, 'req-r2', 'survivable_pair')['status'] == 'blocked'


def test_large_grid_overlapping_groups_and_idempotency(tmp_path):
    w = h = 20
    data = [[0.0] * w for _ in range(h)]
    for r in (5, 10, 15):
        data[r] = [1.0] * w
    for r in range(5, 16):
        data[r][0] = data[r][19] = 1.0
    db, rid = make_grid_run(tmp_path, data)
    # Overlapping frozen groups: mid+top share G0, mid+bottom share G1, so the
    # cheapest disjoint pair is not survivable; top+bottom is.
    groups = [[[10, 7], [5, 5]], [[10, 7], [15, 10]]]
    assert {tuple(c) for c in groups[0]} & {tuple(c) for c in groups[1]}
    corridor.register(db, 'req-big', rid, [10, 0], [10, 19], 29, groups)
    r1 = corridor.corridor_assessment(db, 'req-big', 'survivable_pair')
    assert r1['status'] == 'viable'
    assert sorted(r['cost'] for r in r1['routes']) == [29.0, 29.0]
    assert len(r1['group_intersections']['0']) == 1
    assert len(r1['group_intersections']['1']) == 1
    first, second = ({tuple(c) for c in r['cells']} for r in r1['routes'])
    assert first & second == {(10, 0), (10, 19)}
    for route in r1['routes']:
        cells = route['cells']
        assert cells[0] == [10, 0] and cells[-1] == [10, 19]
        for a, b in zip(cells, cells[1:]):
            assert abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1
    r2 = corridor.corridor_assessment(db, 'req-big', 'survivable_pair')
    assert r1 == r2
    assert connect(db).execute(
        'SELECT count(*) c FROM corridor_assessment WHERE request_id=?',
        ('req-big',)).fetchone()['c'] == 1


def test_pair_never_copies_one_route_and_endpoint_groups_block(tmp_path):
    # Start and goal are adjacent: the direct edge has no interior cells, so
    # only an explicit guard stops the pair from being one path copied twice.
    db, rid = make_grid_run(tmp_path, [[1.0, 1.0], [1.0, 1.0]])
    corridor.register(db, 'req-edge', rid, [0, 0], [0, 1], 10)
    result = corridor.corridor_assessment(db, 'req-edge', 'survivable_pair')
    assert result['status'] == 'viable'
    paths = [r['cells'] for r in result['routes']]
    assert paths[0] != paths[1]
    assert sorted(r['cost'] for r in result['routes']) == [1.0, 3.0]
    # A frozen group containing an endpoint meets every possible pair.
    corridor.register(db, 'req-endpoint', rid, [0, 0], [0, 1], 10, [[[0, 0], [1, 1]]])
    assert corridor.corridor_assessment(db, 'req-endpoint', 'survivable_pair')['status'] == 'blocked'


def test_uninterpretable_contract_never_viable(tmp_path):
    db, rid = make_grid_run(tmp_path, [[1.0] * 5] * 5, kind='extensive', method='area')
    corridor.register(db, 'req-x', rid, [0, 0], [4, 4], 100)
    result = corridor.corridor_assessment(db, 'req-x', 'single_fast')
    assert result['status'] == 'unverifiable'
    assert result['reason'] == 'contract_uninterpretable'


def test_assessment_rejects_unknown_request_and_policy(tmp_path):
    db, rid = corridor_5x5(tmp_path)
    corridor.register(db, 'req-v', rid, [2, 0], [2, 4], 10)
    with pytest.raises(ValueError, match='unknown_policy'):
        corridor.corridor_assessment(db, 'req-v', 'warp_speed')
    with pytest.raises(ValueError, match='unknown_corridor_request'):
        corridor.corridor_assessment(db, 'nope', 'single_fast')


def test_cli_corridor_assess(tmp_path, capsys, monkeypatch):
    db, rid = corridor_5x5(tmp_path)
    corridor.register(db, 'req-cli', rid, [2, 0], [2, 4], 10)
    from gridreproject import __main__
    monkeypatch.setattr('sys.argv', ['gridreproject', 'corridor-assess', '--db', str(db),
                                     '--id', 'req-cli', '--policy', 'single_fast'])
    __main__.main()
    assert 'viable' in capsys.readouterr().out
