"""Freeze a traversability corridor request, find one shortest route, and
assess redundancy policies over known / unknown / obstacle cells."""
import heapq
import itertools
import json
import math
from pathlib import Path
from .store import connect
from .engine import audit, sha

SCHEMA = """CREATE TABLE IF NOT EXISTS corridor_request(
 id TEXT PRIMARY KEY, run_id INTEGER NOT NULL, start_cell TEXT NOT NULL,
 goal_cell TEXT NOT NULL, max_cost REAL NOT NULL, failure_groups TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS corridor_result(
 request_id TEXT PRIMARY KEY, status TEXT NOT NULL, cost REAL, cells TEXT);
CREATE TABLE IF NOT EXISTS corridor_assessment(
 request_id TEXT NOT NULL, policy TEXT NOT NULL, status TEXT NOT NULL,
 reason TEXT NOT NULL, fingerprint TEXT NOT NULL, detail TEXT NOT NULL,
 created TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(request_id, policy));
"""

POLICIES = ('single_fast', 'survivable_pair')
NEIGHBORS = ((-1, 0), (1, 0), (0, -1), (0, 1))


def register(db_path, request_id, run_id, start, goal, max_cost, failure_groups=None):
    if (not request_id or start == goal or len(start) != 2 or len(goal) != 2 or
            max_cost <= 0 or any(int(v) != v or v < 0 for v in (*start, *goal))):
        raise ValueError('invalid_corridor_request')
    groups = failure_groups or []
    if not isinstance(groups, list) or any(not isinstance(g, list) or not g for g in groups):
        raise ValueError('invalid_failure_groups')
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        if not db.execute('SELECT 1 FROM run WHERE id=?', (run_id,)).fetchone():
            raise ValueError('unknown_run')
        body = (run_id, json.dumps(start), json.dumps(goal), float(max_cost),
                json.dumps(groups, sort_keys=True))
        with db:
            old = db.execute('SELECT run_id,start_cell,goal_cell,max_cost,failure_groups '
                             'FROM corridor_request WHERE id=?', (request_id,)).fetchone()
            if old and tuple(old) != body:
                raise ValueError('corridor_request_conflict')
            db.execute('INSERT OR IGNORE INTO corridor_request VALUES (?,?,?,?,?,?)',
                       (request_id, *body))
        return {'id': request_id}
    finally:
        db.close()


def search(db_path, request_id):
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        request = db.execute('SELECT * FROM corridor_request WHERE id=?', (request_id,)).fetchone()
        if not request:
            raise ValueError('unknown_corridor_request')
        if audit(db_path, request['run_id']):
            return {'status': '待核', 'cost': None, 'cells': []}
        cells = {(row['row'], row['col']): row['value'] for row in db.execute(
            'SELECT row,col,value FROM cell WHERE run_id=?', (request['run_id'],))
            if row['value'] is not None and row['value'] > 0}
        start, goal = tuple(json.loads(request['start_cell'])), tuple(json.loads(request['goal_cell']))
        queue = [(0.0, start, [start])]
        best = {start: 0.0}
        chosen = None
        while queue:
            cost, current, path = heapq.heappop(queue)
            if cost != best[current]:
                continue
            if current == goal:
                chosen = (cost, path)
                break
            for neighbor in ((current[0]-1,current[1]), (current[0]+1,current[1]),
                             (current[0],current[1]-1), (current[0],current[1]+1)):
                if neighbor not in cells:
                    continue
                new_cost = cost + cells[neighbor]
                if new_cost < best.get(neighbor, float('inf')):
                    best[neighbor] = new_cost
                    heapq.heappush(queue, (new_cost, neighbor, path+[neighbor]))
        cost, path = chosen if chosen else (None, [])
        status = '可通行' if cost is not None and cost <= request['max_cost'] else '不可通行'
        with db:
            db.execute('INSERT INTO corridor_result VALUES (?,?,?,?) ON CONFLICT(request_id) '
                       'DO UPDATE SET status=excluded.status,cost=excluded.cost,cells=excluded.cells',
                       (request_id, status, cost, json.dumps(path)))
        return {'status': status, 'cost': cost, 'cells': path}
    finally:
        db.close()


# --- Policy assessment over known / unknown / obstacle cells ----------------
#
# A cell is a known traversable cell only when its audited value is positive,
# finite and backed by coverage meeting the run's frozen contract; a
# non-positive finite value is a known obstacle. Anything else (NULL value,
# coverage below the contract, or inconsistent evidence) is unknown: never a
# zero-cost shortcut in the known graph, never a certain obstacle. The
# optimistic upper-bound graph admits unknown cells at cost 0 and is used only
# to prove impossibility, never to witness a usable route.


def _file_sha(path):
    try:
        return sha(Path(path).read_bytes())
    except OSError:
        return None


def _interpret_contract(contract, source_row):
    """The cost raster is only meaningful as a continuous bilinear product on a
    readable target grid with a usable cost unit."""
    try:
        width, height = int(contract['width']), int(contract['height'])
        min_coverage = float(contract['min_coverage'])
        bounds = contract['bounds']
        if (width <= 0 or height <= 0 or not 0 <= min_coverage <= 1 or
                contract.get('kind') != 'continuous' or
                contract.get('method') != 'bilinear' or
                len(bounds) != 4 or not bounds[2] > bounds[0] or
                not bounds[3] > bounds[1]):
            return None
        unit = source_row['unit'] if source_row else None
        if not isinstance(unit, str) or not unit.strip():
            return None
        return {'width': width, 'height': height, 'min_coverage': min_coverage}
    except (KeyError, TypeError, ValueError):
        return None


def _classify(rows, width, height, min_coverage):
    cost, obstacle, unknown, coverage = {}, set(), set(), {}
    for r in range(height):
        for c in range(width):
            row = rows.get((r, c))
            if row is None:
                unknown.add((r, c))
                continue
            value, cov = row['value'], row['coverage']
            coverage[(r, c)] = cov
            if (value is None or cov is None or cov < min_coverage or
                    not math.isfinite(value)):
                unknown.add((r, c))
            elif value > 0:
                cost[(r, c)] = float(value)
            else:
                obstacle.add((r, c))
    return cost, obstacle, unknown, coverage


def _shortest(cost, start, goal, banned_nodes=frozenset(), banned_edges=frozenset()):
    """Cheapest 4-neighbour route; entering a cell adds its cost."""
    if start not in cost or goal not in cost or start in banned_nodes or goal in banned_nodes:
        return None
    counter = itertools.count()
    queue = [(0.0, next(counter), start, (start,))]
    best = {start: 0.0}
    while queue:
        total, _, node, path = heapq.heappop(queue)
        if total > best.get(node, math.inf):
            continue
        if node == goal:
            return total, list(path)
        for dr, dc in NEIGHBORS:
            neighbor = (node[0] + dr, node[1] + dc)
            if neighbor not in cost or neighbor in banned_nodes:
                continue
            if frozenset((node, neighbor)) in banned_edges:
                continue
            new = total + cost[neighbor]
            if new < best.get(neighbor, math.inf):
                best[neighbor] = new
                heapq.heappush(queue, (new, next(counter), neighbor, path + (neighbor,)))
    return None


def _dist_to_goal(cost, goal):
    """Exact remaining entering-cost from every cell to goal (admissible bound)."""
    if goal not in cost:
        return {}
    dist = {goal: 0.0}
    queue = [(0.0, goal)]
    while queue:
        total, node = heapq.heappop(queue)
        if total > dist.get(node, math.inf):
            continue
        for dr, dc in NEIGHBORS:
            neighbor = (node[0] + dr, node[1] + dc)
            if neighbor not in cost:
                continue
            new = total + cost[node]
            if new < dist.get(neighbor, math.inf):
                dist[neighbor] = new
                heapq.heappush(queue, (new, neighbor))
    return dist


def _find_pair(cost, start, goal, max_cost, group_sets, max_expansions=2_000_000):
    """Find two routes that share no cell but the endpoints, each within
    max_cost, with every frozen failure group meeting at most one of them.

    Complete decision procedure: best-first enumeration of budget-bounded
    simple paths for the first route (pruned by the exact distance-to-goal
    lower bound), then an exact shortest-path search for the second route on
    the graph minus the first route's cells and the groups it hit. Any
    qualifying pair can be reduced to such simple paths, so exhaustion proves
    impossibility; unbounded enumeration is avoided by the budget and bound.
    """
    if start not in cost or goal not in cost:
        return None
    cell_group = {}
    for index, cells in enumerate(group_sets):
        if start in cells or goal in cells:
            return None  # every route pair shares the failed endpoint
        for cell in cells:
            cell_group[cell] = cell_group.get(cell, 0) | (1 << index)
    dist = _dist_to_goal(cost, goal)
    counter = itertools.count()
    queue = [(0.0, next(counter), start, (start,), frozenset((start,)),
              cell_group.get(start, 0))]
    expansions = 0
    while queue:
        total, _, node, path, seen, mask = heapq.heappop(queue)
        expansions += 1
        if expansions > max_expansions:
            raise RuntimeError('corridor_search_space_exceeded')
        if node == goal:
            banned = set(path[1:-1])
            for index, cells in enumerate(group_sets):
                if (mask >> index) & 1:
                    banned |= cells
            banned -= {start, goal}
            # A direct start-goal route has no interior cells to ban; forbid
            # reusing the same edge so the pair is never one path copied twice.
            edges = {frozenset((start, goal))} if len(path) == 2 else frozenset()
            second = _shortest(cost, start, goal, banned, edges)
            if second and second[0] <= max_cost and second[1] != list(path):
                return (total, list(path)), second
            continue
        for dr, dc in NEIGHBORS:
            neighbor = (node[0] + dr, node[1] + dc)
            if neighbor not in cost or neighbor in seen:
                continue
            new = total + cost[neighbor]
            if new > max_cost or new + dist.get(neighbor, math.inf) > max_cost:
                continue
            heapq.heappush(queue, (new, next(counter), neighbor,
                                   path + (neighbor,), seen | {neighbor},
                                   mask | cell_group.get(neighbor, 0)))
    return None


def _evaluate_policy(cost, unknown, start, goal, max_cost, group_sets, policy):
    """Decide one policy on the known graph, else on the optimistic bound."""
    upper = dict(cost)
    for cell in unknown:
        upper[cell] = 0.0
    if policy == 'single_fast':
        known = _shortest(cost, start, goal)
        if known and known[0] <= max_cost:
            return 'viable', (known,), {}
        bound = _shortest(upper, start, goal)
        if bound is None or bound[0] > max_cost:
            return 'blocked', None, {'upper_min_cost': None if bound is None else bound[0]}
        return 'indeterminate', (bound,), {}
    known = _find_pair(cost, start, goal, max_cost, group_sets)
    if known:
        return 'viable', known, {}
    bound = _find_pair(upper, start, goal, max_cost, group_sets)
    if bound is None:
        return 'blocked', None, {}
    return 'indeterminate', bound, {}


def _verify_witness(routes, cost, group_sets, start, goal, max_cost):
    """Refuse to publish a witness that violates movement, budget, disjointness
    or the shared-failure-group rule."""
    def ensure(ok):
        if not ok:
            raise RuntimeError('corridor_witness_invalid')
    cell_sets = []
    for route in routes:
        path = [tuple(c) for c in route['cells']]
        ensure(path[0] == start and path[-1] == goal and len(path) >= 2)
        ensure(all((b[0] - a[0], b[1] - a[1]) in NEIGHBORS for a, b in zip(path, path[1:])))
        ensure(all(p in cost for p in path))
        total = sum(cost[p] for p in path[1:])
        ensure(abs(total - route['cost']) <= 1e-9 and total <= max_cost)
        cell_sets.append(set(path))
    if len(routes) == 2:
        ensure(cell_sets[0] != cell_sets[1])
        ensure(cell_sets[0] & cell_sets[1] <= {start, goal})
        for cells in group_sets:
            ensure(not (cell_sets[0] & cells and cell_sets[1] & cells))


def _assess(request, policy, contract, source_row, rows, issues, fingerprint, frozen):
    start = tuple(json.loads(request['start_cell']))
    goal = tuple(json.loads(request['goal_cell']))
    max_cost = float(request['max_cost'])
    group_sets = [frozenset(tuple(cell) for cell in group)
                  for group in json.loads(request['failure_groups'])]
    result = {'request_id': request['id'], 'policy': policy, 'run_id': request['run_id'],
              'status': None, 'reason': None, 'max_cost': max_cost,
              'routes': [], 'group_intersections': {}, 'unknown_cells': [],
              'evidence': {}, 'audit': issues,
              'groups_digest': sha(request['failure_groups'].encode()),
              'frozen': frozen, 'fingerprint': fingerprint}
    if issues:
        result.update(status='unverifiable', reason='audit_failed')
        return result
    grid = _interpret_contract(contract, source_row)
    if grid is None:
        result.update(status='unverifiable', reason='contract_uninterpretable')
        return result
    by_cell = {(row['row'], row['col']): row for row in rows}
    cost, _, unknown, coverage = _classify(by_cell, grid['width'], grid['height'],
                                           grid['min_coverage'])
    status, witness, extra = _evaluate_policy(cost, unknown, start, goal, max_cost,
                                              group_sets, policy)
    if status == 'viable':
        routes = []
        for route_cost, path in witness:
            hit = sorted(i for i, cells in enumerate(group_sets)
                         if any(tuple(c) in cells for c in path))
            routes.append({'cells': [list(c) for c in path], 'cost': route_cost,
                           'coverage': [coverage[tuple(c)] for c in path],
                           'groups': hit})
        _verify_witness(routes, cost, group_sets, start, goal, max_cost)
        intersections = {str(i): [k for k, route in enumerate(routes) if i in route['groups']]
                         for i in range(len(group_sets))}
        result.update(status='viable', reason='witnessed_in_known_graph', routes=routes,
                      group_intersections=intersections, evidence={'graph': 'known'})
    elif status == 'blocked':
        result.update(status='blocked', reason='infeasible_in_upper_bound',
                      evidence={'graph': 'upper_bound', **extra})
    else:
        decisive = sorted({tuple(c) for _, path in witness for c in path} & unknown)
        result.update(status='indeterminate', reason='unknown_cells_decisive',
                      unknown_cells=[list(c) for c in decisive],
                      evidence={'graph': 'upper_bound',
                                'cost_lower_bounds': [w[0] for w in witness]})
    return result


def corridor_assessment(db_path, request_id, policy):
    """Assess a frozen corridor request under one redundancy policy.

    viable: a policy-satisfying route (pair) exists in the known graph and is
    returned with per-cell coverage and per-group failure intersections.
    blocked: even the optimistic upper-bound graph cannot satisfy the policy.
    indeterminate: the known graph fails but the upper bound allows it; the
    decisive unknown cells are reported. Audit failures or an uninterpretable
    cost grid never yield viable. Results freeze the run/contract/source/
    output evidence, are idempotent while it stands, and are never reused
    after the raster or the cell evidence changes. corridor_result is not
    touched by this evaluation.
    """
    if policy not in POLICIES:
        raise ValueError('unknown_policy')
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        request = db.execute('SELECT * FROM corridor_request WHERE id=?', (request_id,)).fetchone()
        if not request:
            raise ValueError('unknown_corridor_request')
        run_id = request['run_id']
    finally:
        db.close()
    try:
        issues = list(audit(db_path, run_id))
    except ValueError:
        issues = ['no_output']
    db = connect(db_path)
    try:
        run = db.execute('SELECT * FROM run WHERE id=?', (run_id,)).fetchone()
        contract_row = db.execute('SELECT * FROM contract WHERE id=?',
                                  (run['contract_id'],)).fetchone()
        source_row = db.execute('SELECT * FROM source WHERE id=?',
                                (run['source_id'],)).fetchone()
        output_row = db.execute('SELECT * FROM output WHERE run_id=?', (run_id,)).fetchone()
        rows = db.execute('SELECT row,col,value,coverage FROM cell WHERE run_id=? '
                          'ORDER BY row,col', (run_id,)).fetchall()
        contract = json.loads(contract_row['body']) if contract_row else {}
        cells_digest = sha(json.dumps(
            [[r['row'], r['col'], r['value'], r['coverage']] for r in rows],
            separators=(',', ':')).encode())
        frozen = {'run_id': run_id,
                  'contract_hash': contract_row['hash'] if contract_row else None,
                  'source_hash': source_row['hash'] if source_row else None,
                  'output_hash': output_row['hash'] if output_row else None}
        fingerprint = sha(json.dumps({
            'request_id': request_id, 'policy': policy, 'run_id': run_id,
            'start': request['start_cell'], 'goal': request['goal_cell'],
            'max_cost': request['max_cost'], 'groups': request['failure_groups'],
            'contract_hash': frozen['contract_hash'],
            'source_hash': frozen['source_hash'], 'output_hash': frozen['output_hash'],
            'source_now': _file_sha(source_row['path']) if source_row else None,
            'output_now': _file_sha(output_row['path']) if output_row else None,
            'cells': cells_digest, 'audit': sorted(issues),
        }, sort_keys=True, separators=(',', ':')).encode())
        old = db.execute('SELECT fingerprint, detail FROM corridor_assessment '
                         'WHERE request_id=? AND policy=?', (request_id, policy)).fetchone()
        if old and old['fingerprint'] == fingerprint:
            return json.loads(old['detail'])
        result = _assess(request, policy, contract, source_row, rows, issues,
                         fingerprint, frozen)
        with db:
            db.execute('INSERT INTO corridor_assessment'
                       '(request_id,policy,status,reason,fingerprint,detail)'
                       ' VALUES (?,?,?,?,?,?) ON CONFLICT(request_id,policy) DO UPDATE SET'
                       ' status=excluded.status,reason=excluded.reason,'
                       ' fingerprint=excluded.fingerprint,detail=excluded.detail',
                       (request_id, policy, result['status'], result['reason'],
                        fingerprint, json.dumps(result, sort_keys=True)))
        return result
    finally:
        db.close()
