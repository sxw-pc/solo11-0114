"""Three-state corridor policy assessment over audited cost rasters."""
import json
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds
from gridreproject import engine, corridor, __main__
from gridreproject.store import connect

NODATA = -9999.0


def make_grid(tmp_path, values, name='grid', db=None, min_coverage=1.0,
              kind='continuous', method='bilinear'):
    """成本/障碍/缺测 fixture:None 写成 NoData(缺测),0 是确定障碍。"""
    height, width = len(values), len(values[0])
    data = np.array([[NODATA if v is None else float(v) for v in row]
                     for row in values], dtype='float64')
    source = tmp_path / f'{name}.tif'
    with rasterio.open(source, 'w', driver='GTiff', width=width, height=height,
                       count=1, dtype='float64', crs='EPSG:4326',
                       transform=from_bounds(0, 0, width, height, width, height),
                       nodata=NODATA) as dst:
        dst.write(data, 1)
    db = db or (tmp_path / f'{name}.db')
    contract = {'crs': 'EPSG:4326', 'bounds': [0, 0, width, height],
                'width': width, 'height': height, 'kind': kind,
                'method': method, 'min_coverage': min_coverage}
    rid = engine.run(db, source, contract, tmp_path / f'{name}-out.tif')
    return db, rid


def corridor5():
    """5×5:只有一条从左端到右端的值为 1 的连续走廊,其余为确定障碍。"""
    grid = [[0.0] * 5 for _ in range(5)]
    grid[2] = [1.0] * 5
    return grid


def trap_grid():
    """三条候选路线:A(上行,最便宜)、B(顶行,次便宜)、C(底行,较贵)。"""
    return [[1.0] * 5, [1.0] * 5, [2.0] * 5]


def assert_legal_route(route, start, goal, max_cost):
    cells = [tuple(c) for c in route['cells']]
    assert cells[0] == tuple(start) and cells[-1] == tuple(goal)
    assert len(set(cells)) == len(cells)
    for a, b in zip(cells, cells[1:]):
        assert abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1
    assert route['cost'] <= max_cost
    assert len(route['coverage']) == len(cells)
    assert all(0.0 <= c <= 1.0 for c in route['coverage'])


def assert_survivable(result, start, goal, max_cost, groups):
    assert result['status'] == 'viable' and len(result['routes']) == 2
    paths = []
    for route in result['routes']:
        assert_legal_route(route, start, goal, max_cost)
        paths.append({tuple(c) for c in route['cells']})
    assert (paths[0] & paths[1]) <= {tuple(start), tuple(goal)}
    for group in groups:
        cells = {tuple(c) for c in group}
        assert not (cells & paths[0] and cells & paths[1])


def strip_cache(result):
    return {k: v for k, v in result.items() if k != 'cached'}


def test_single_fast_viable_and_survivable_pair_blocked(tmp_path):
    db, rid = make_grid(tmp_path, corridor5())
    corridor.register(db, 'req', rid, [2, 0], [2, 4], 10)
    single = corridor.corridor_assessment(db, 'req', 'single_fast')
    assert single['status'] == 'viable' and single['reason'] == 'ok'
    assert single['routes'][0]['cells'] == [[2, c] for c in range(5)]
    assert single['routes'][0]['cost'] == 4
    assert_legal_route(single['routes'][0], [2, 0], [2, 4], 10)
    pair = corridor.corridor_assessment(db, 'req', 'survivable_pair')
    assert pair['status'] == 'blocked' and pair['reason'] == 'disconnected'
    assert pair['routes'] == []
    assert pair['evidence']['proof'] == {'kind': 'max_flow', 'value': 1}


def test_unknown_bypass_makes_pair_indeterminate_not_blocked(tmp_path):
    grid = corridor5()
    grid[1] = [None] * 5  # 一条可能的旁路改为缺测
    db, rid = make_grid(tmp_path, grid)
    corridor.register(db, 'req', rid, [2, 0], [2, 4], 10)
    pair = corridor.corridor_assessment(db, 'req', 'survivable_pair')
    assert pair['status'] == 'indeterminate' and pair['reason'] == 'unknown_cells'
    assert pair['routes'] == []
    assert sorted(map(tuple, pair['unknown_cells'])) == [(1, c) for c in range(5)]
    upper = pair['evidence']['upper_bound_routes']
    assert len(upper) == 2 and 'upper bound' in pair['evidence']['note']
    single = corridor.corridor_assessment(db, 'req', 'single_fast')
    assert single['status'] == 'viable'


def test_unknown_on_only_corridor_is_three_state(tmp_path):
    grid = corridor5()
    grid[2][2] = None  # 唯一走廊上的缺测格
    db, rid = make_grid(tmp_path, grid)
    corridor.register(db, 'req', rid, [2, 0], [2, 4], 10)
    single = corridor.corridor_assessment(db, 'req', 'single_fast')
    assert single['status'] == 'indeterminate'
    assert single['unknown_cells'] == [[2, 2]]
    assert single['evidence']['upper_bound_cost'] == 3
    pair = corridor.corridor_assessment(db, 'req', 'survivable_pair')
    assert pair['status'] == 'blocked'


def test_unknown_cells_are_neither_shortcut_nor_obstacle(tmp_path):
    grid = corridor5()
    grid[2][2] = 0.0  # 走廊上的确定障碍
    grid[0][0] = None  # 孤立的缺测格救不了路线
    db, rid = make_grid(tmp_path, grid)
    corridor.register(db, 'req', rid, [2, 0], [2, 4], 10)
    single = corridor.corridor_assessment(db, 'req', 'single_fast')
    assert single['status'] == 'blocked' and single['reason'] == 'disconnected'


def test_low_coverage_cell_is_unknown_not_obstacle(tmp_path):
    db, rid = make_grid(tmp_path, corridor5())
    # 覆盖低于冻结合同 min_coverage 的格是未知;审计不逐格复核覆盖。
    with connect(db) as ledger:
        ledger.execute('UPDATE cell SET coverage=0.5 WHERE run_id=? AND row=2 AND col=2', (rid,))
    corridor.register(db, 'req', rid, [2, 0], [2, 4], 10)
    single = corridor.corridor_assessment(db, 'req', 'single_fast')
    assert single['status'] == 'indeterminate'
    assert single['unknown_cells'] == [[2, 2]]


def test_budget_boundary_and_path_legality(tmp_path):
    db, rid = make_grid(tmp_path, corridor5())
    corridor.register(db, 'exact', rid, [2, 0], [2, 4], 4)
    exact = corridor.corridor_assessment(db, 'exact', 'single_fast')
    assert exact['status'] == 'viable' and exact['routes'][0]['cost'] == 4
    corridor.register(db, 'tight', rid, [2, 0], [2, 4], 3)
    tight = corridor.corridor_assessment(db, 'tight', 'single_fast')
    assert tight['status'] == 'blocked' and tight['reason'] == 'budget_exceeded'
    assert tight['evidence']['proof']['optimistic_cost'] == 4


def test_shared_failure_group_defeats_two_cheapest_routes(tmp_path):
    """两条最便宜路线各自不共格却穿过同一失效组;较贵的第三条才可存活。

    逐次运行两遍 Dijkstra 取前两条会错判:naive 配对 (A,B) 共享失效组,
    正确配对是 (A,C),其中 C 是更贵的第三条路线。
    """
    db, rid = make_grid(tmp_path, trap_grid())
    groups = [[[1, 2], [0, 2]]]  # A 与 B 都穿过该组
    corridor.register(db, 'trap', rid, [1, 0], [1, 4], 11, groups)
    result = corridor.corridor_assessment(db, 'trap', 'survivable_pair')
    assert_survivable(result, [1, 0], [1, 4], 11, groups)
    costs = sorted(route['cost'] for route in result['routes'])
    assert costs == [4, 11]  # 不是两条最便宜的 (4 和 6)
    hits = result['evidence']['group_hits']['0']
    assert len(hits) == 1
    # naive 两遍 Dijkstra 的前两条确实共享失效组
    known = {(r, c): 1.0 for r in range(2) for c in range(5)}
    known.update({(2, c): 2.0 for c in range(5)})
    _, first = corridor._shortest(set(known), known, (1, 0), (1, 4), 11)
    _, second = corridor._shortest(set(known), known, (1, 0), (1, 4), 11,
                                   banned=set(first) - {(1, 0), (1, 4)})
    assert {(1, 2), (0, 2)} & set(first) and {(1, 2), (0, 2)} & set(second)


def test_pair_blocked_when_survivable_option_exceeds_budget(tmp_path):
    # 预算 8 时,绕开失效组的路线至少花费 9,所有预算内路线都穿过该组。
    db, rid = make_grid(tmp_path, trap_grid())
    groups = [[[1, 2], [0, 2]]]
    corridor.register(db, 'trap', rid, [1, 0], [1, 4], 8, groups)
    result = corridor.corridor_assessment(db, 'trap', 'survivable_pair')
    assert result['status'] == 'blocked' and result['reason'] == 'no_survivable_pair'
    assert result['evidence']['proof']['kind'] == 'exhaustive_search'
    single = corridor.corridor_assessment(db, 'trap', 'single_fast')
    assert single['status'] == 'viable' and single['routes'][0]['cost'] == 4


def test_failure_group_on_endpoint_blocks_pair(tmp_path):
    db, rid = make_grid(tmp_path, corridor5())
    corridor.register(db, 'req', rid, [2, 0], [2, 4], 10, [[[2, 0]]])
    pair = corridor.corridor_assessment(db, 'req', 'survivable_pair')
    assert pair['status'] == 'blocked' and pair['reason'] == 'no_survivable_pair'
    assert pair['evidence']['proof']['kind'] == 'endpoint_in_failure_group'


def test_large_grid_overlapping_groups_and_idempotency(tmp_path):
    n = 20
    db, rid = make_grid(tmp_path, [[1.0] * n for _ in range(n)], name='big')
    groups = [[[5 + i, 8], [5 + i, 9], [5 + i, 10], [6 + i, 9]] for i in range(8)]
    corridor.register(db, 'big', rid, [0, 0], [19, 19], 38, groups)
    first = corridor.corridor_assessment(db, 'big', 'survivable_pair')
    assert not first['cached']
    assert_survivable(first, [0, 0], [19, 19], 38, groups)
    assert sorted(first['evidence']['costs']) == [38, 38]
    second = corridor.corridor_assessment(db, 'big', 'survivable_pair')
    assert second['cached'] and strip_cache(second) == strip_cache(first)
    ledger = connect(db)
    assert ledger.execute('SELECT count(*) FROM corridor_assessment').fetchone()[0] == 1
    row = ledger.execute('SELECT * FROM corridor_assessment').fetchone()
    assert row['groups_digest'] == corridor._groups_digest(groups)
    assert row['run_id'] == rid and row['contract_hash'] and row['source_hash']
    single = corridor.corridor_assessment(db, 'big', 'single_fast')
    assert single['status'] == 'viable' and single['routes'][0]['cost'] == 38


def test_large_grid_budget_and_bottleneck_counterexamples(tmp_path):
    n = 20
    db, rid = make_grid(tmp_path, [[1.0] * n for _ in range(n)], name='big')
    corridor.register(db, 'tight', rid, [0, 0], [19, 19], 37)
    pair = corridor.corridor_assessment(db, 'tight', 'survivable_pair')
    assert pair['status'] == 'blocked' and pair['reason'] == 'budget_exceeded'
    wall = [[1.0] * n for _ in range(n)]
    for r in range(n):
        if r != 5:
            wall[r][10] = 0.0  # 单格缺口形成瓶颈
    db2, rid2 = make_grid(tmp_path, wall, name='wall')
    corridor.register(db2, 'wall', rid2, [0, 0], [19, 19], 100)
    blocked = corridor.corridor_assessment(db2, 'wall', 'survivable_pair')
    assert blocked['status'] == 'blocked' and blocked['reason'] == 'disconnected'
    single = corridor.corridor_assessment(db2, 'wall', 'single_fast')
    assert single['status'] == 'viable'


def test_tamper_invalidates_and_corridor_result_untouched(tmp_path):
    db, rid = make_grid(tmp_path, corridor5())
    corridor.register(db, 'req', rid, [2, 0], [2, 4], 10)
    found = corridor.search(db, 'req')
    assert found['status'] == '可通行'
    before = [dict(r) for r in connect(db).execute('SELECT * FROM corridor_result')]
    ok = corridor.corridor_assessment(db, 'req', 'single_fast')
    assert ok['status'] == 'viable' and not ok['cached']
    again = corridor.corridor_assessment(db, 'req', 'single_fast')
    assert again['cached'] and strip_cache(again) == strip_cache(ok)
    corridor.corridor_assessment(db, 'req', 'survivable_pair')
    after = [dict(r) for r in connect(db).execute('SELECT * FROM corridor_result')]
    assert before == after  # 新功能不写 corridor_result
    out = tmp_path / 'grid-out.tif'
    original = out.read_bytes()
    out.write_bytes(b'tampered')
    bad = corridor.corridor_assessment(db, 'req', 'single_fast')
    assert bad['status'] != 'viable' and bad['reason'] == 'audit_failed'
    assert 'output_hash' in bad['evidence']['audit_issues']
    out.write_bytes(original)
    restored = corridor.corridor_assessment(db, 'req', 'single_fast')
    assert restored['status'] == 'viable' and not restored['cached']


def test_uninterpretable_cost_grid_never_viable(tmp_path):
    db, rid = make_grid(tmp_path, corridor5(), name='cat',
                        kind='category', method='nearest')
    corridor.register(db, 'req', rid, [2, 0], [2, 4], 10)
    result = corridor.corridor_assessment(db, 'req', 'single_fast')
    assert result['status'] == 'indeterminate'
    assert result['reason'] == 'uninterpretable_cost_grid'


def test_unknown_policy_and_request_rejected(tmp_path):
    db, rid = make_grid(tmp_path, corridor5())
    corridor.register(db, 'req', rid, [2, 0], [2, 4], 10)
    with pytest.raises(ValueError, match='unknown_corridor_policy'):
        corridor.corridor_assessment(db, 'req', 'banana')
    with pytest.raises(ValueError, match='unknown_corridor_request'):
        corridor.corridor_assessment(db, 'nope', 'single_fast')


def test_cli_corridor_assess(tmp_path, monkeypatch, capsys):
    db, rid = make_grid(tmp_path, corridor5())
    corridor.register(db, 'req', rid, [2, 0], [2, 4], 10)
    monkeypatch.setattr('sys.argv', ['gridreproject', 'corridor-assess',
                                     '--db', str(db), '--id', 'req',
                                     '--policy', 'single_fast'])
    __main__.main()
    assert 'viable' in capsys.readouterr().out
