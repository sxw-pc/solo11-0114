"""Freeze a traversability corridor request and assess route policies.

`search` keeps the original single-route lowest-cost semantics on the
audited continuous cost raster and writes `corridor_result`.
`corridor_assessment` is an independent three-state evaluation
(viable / blocked / indeterminate) for the `single_fast` and
`survivable_pair` policies and never writes `corridor_result`.

Cells of the frozen run split into known-traversable (positive finite
value with contract-level coverage), known-obstacle (non-positive
value) and unknown (missing value, coverage below the frozen contract
minimum, or inconsistent evidence). Unknown cells are never free
shortcuts in the certified graph and never assumed real obstacles: they
become passable at zero cost only in the optimistic upper-bound graph
that proves impossibility. Assessment conclusions are frozen with the
run/contract/source/output hashes, the policy and the failure-group
digest, so repeated evaluation is idempotent and later tampering or
re-runs never reuse stale verdicts.
"""
import heapq
import itertools
import json
import math
from collections import deque
from pathlib import Path
from .store import connect, cell_map
from .engine import audit, sha

SCHEMA = """CREATE TABLE IF NOT EXISTS corridor_request(
 id TEXT PRIMARY KEY, run_id INTEGER NOT NULL, start_cell TEXT NOT NULL,
 goal_cell TEXT NOT NULL, max_cost REAL NOT NULL, failure_groups TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS corridor_result(
 request_id TEXT PRIMARY KEY, status TEXT NOT NULL, cost REAL, cells TEXT);
"""


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


POLICIES = ('single_fast', 'survivable_pair')
KNOWN, OBSTACLE, UNKNOWN = 'known', 'obstacle', 'unknown'
_BLOCKED_REASON = {'endpoint_unusable': 'disconnected',
                   'endpoint_in_failure_group': 'no_survivable_pair',
                   'max_flow': 'disconnected',
                   'group_cut': 'no_survivable_pair',
                   'budget': 'budget_exceeded',
                   'exhaustive_search': 'no_survivable_pair'}


def _neighbors(cell):
    row, col = cell
    return ((row, col + 1), (row + 1, col), (row, col - 1), (row - 1, col))


def _normalize_groups(groups):
    normalized = []
    for group in groups:
        cells = set()
        for cell in group:
            row, col = cell
            cells.add((int(row), int(col)))
        normalized.append(sorted(cells))
    normalized.sort()
    return normalized


def _groups_digest(groups):
    return sha(json.dumps(_normalize_groups(groups)).encode())


def _file_sha(path):
    p = Path(path)
    return sha(p.read_bytes()) if p.is_file() else 'missing'


def _classify_cells(db, run_id, contract):
    """Split the frozen grid into known-traversable, known-obstacle and
    unknown cells. Positive finite values with contract-level coverage
    are traversable, non-positive values are obstacles, and missing
    values, low coverage or inconsistent evidence are unknown."""
    width, height = int(contract['width']), int(contract['height'])
    min_coverage = float(contract['min_coverage'])
    stored = cell_map(db, run_id)
    info = {}
    for row in range(height):
        for col in range(width):
            value, coverage = stored.get((row, col), (None, None))
            coverage_ok = (coverage is not None and math.isfinite(coverage)
                           and 0.0 <= coverage <= 1.0)
            value_ok = value is not None and math.isfinite(value)
            if not coverage_ok or coverage < min_coverage or not value_ok:
                state = UNKNOWN
            elif value <= 0:
                state = OBSTACLE
            else:
                state = KNOWN
            info[(row, col)] = {'state': state,
                                'value': value if value_ok else None,
                                'coverage': coverage if coverage_ok else 0.0}
    return info


def _shortest(passable, cost, start, goal, max_cost, banned=frozenset()):
    """Cheapest four-neighbour route; entering a cell adds its cost and
    the endpoints cannot be bypassed. Returns (cost, path) or (None, None)."""
    if (start not in passable or goal not in passable
            or start in banned or goal in banned):
        return None, None
    dist = {start: 0.0}
    parent = {}
    queue = [(0.0, start)]
    while queue:
        total, cell = heapq.heappop(queue)
        if total > dist.get(cell, math.inf):
            continue
        if cell == goal:
            path = [cell]
            while cell in parent:
                cell = parent[cell]
                path.append(cell)
            return total, path[::-1]
        for neighbor in _neighbors(cell):
            if neighbor not in passable or neighbor in banned:
                continue
            new_total = total + cost[neighbor]
            if new_total > max_cost or new_total >= dist.get(neighbor, math.inf):
                continue
            dist[neighbor] = new_total
            parent[neighbor] = cell
            heapq.heappush(queue, (new_total, neighbor))
    return None, None


def _lower_bounds(passable, cost, goal):
    """Optimistic remaining cost to reach the goal from every cell."""
    if goal not in passable:
        return {}
    dist = {goal: 0.0}
    queue = [(0.0, goal)]
    while queue:
        total, cell = heapq.heappop(queue)
        if total > dist.get(cell, math.inf):
            continue
        for neighbor in _neighbors(cell):
            if neighbor not in passable:
                continue
            new_total = total + cost[cell]
            if new_total < dist.get(neighbor, math.inf):
                dist[neighbor] = new_total
                heapq.heappush(queue, (new_total, neighbor))
    return dist


def _reachable(passable, blocked, start, goal):
    """Whether goal is reachable from start without entering blocked cells;
    the start cell itself is always allowed as the origin."""
    if goal in blocked or start not in passable or goal not in passable:
        return False
    seen = {start}
    stack = [start]
    while stack:
        cell = stack.pop()
        if cell == goal:
            return True
        for neighbor in _neighbors(cell):
            if neighbor in passable and neighbor not in blocked and neighbor not in seen:
                seen.add(neighbor)
                stack.append(neighbor)
    return False


def _max_disjoint(passable, start, goal, limit=2):
    """Count of cell-disjoint routes (endpoints shared), capped at limit."""
    if start not in passable or goal not in passable:
        return 0
    capacity = {}

    def add(tail, head, weight):
        capacity.setdefault(tail, {})[head] = capacity.setdefault(tail, {}).get(head, 0) + weight
        capacity.setdefault(head, {}).setdefault(tail, 0)

    for cell in passable:
        add((cell, 0), (cell, 1), 2 if cell in (start, goal) else 1)
    for cell in passable:
        for neighbor in _neighbors(cell):
            if neighbor in passable:
                add((cell, 1), (neighbor, 0), 1)
    source, sink = (start, 0), (goal, 1)
    flow = 0
    while flow < limit:
        parent = {source: None}
        queue = deque([source])
        while queue and sink not in parent:
            node = queue.popleft()
            for nxt, weight in capacity.get(node, {}).items():
                if weight > 0 and nxt not in parent:
                    parent[nxt] = node
                    queue.append(nxt)
        if sink not in parent:
            break
        node = sink
        while parent[node] is not None:
            prev = parent[node]
            capacity[prev][node] -= 1
            capacity[node][prev] = capacity[node].get(prev, 0) + 1
            node = prev
        flow += 1
    return flow


def _path_cost(path, cost):
    return sum(cost[cell] for cell in path[1:])


def _verify_pair(first, second, start, goal, cost, groups, max_cost):
    for path in (first, second):
        if path[0] != start or path[-1] != goal or len(set(path)) != len(path):
            return False
        if any(abs(a[0] - b[0]) + abs(a[1] - b[1]) != 1 for a, b in zip(path, path[1:])):
            return False
        if _path_cost(path, cost) > max_cost:
            return False
    if (set(first) & set(second)) - {start, goal}:
        return False
    for group in groups:
        cells = set(group)
        if cells & set(first) and cells & set(second):
            return False
    return True


def _find_survivable_pair(passable, cost, start, goal, groups, max_cost):
    """Find two routes that share only the endpoints, each within budget,
    with every frozen failure group meeting at most one of them.

    Complete decision, not unbounded path enumeration: max-flow and
    group-cut prechecks settle common impossible cases, then the first
    route is enumerated with budget, connectivity and claimed-group
    pruning (a prefix is dropped as soon as the cells its failure groups
    claim leave no room for any second route) while the second route is
    an exact cheapest-route query that must avoid the first route's
    cells and every failure group the first route touches. The general
    problem is NP-hard, so adversarial layouts can still be slow, but
    the prechecks and goal-directed ordering decide the 20x20 scale
    quickly. Returns (pair, None) or (None, proof)."""
    if start not in passable or goal not in passable:
        return None, {'kind': 'endpoint_unusable'}
    cell_groups = {}
    group_cells = []
    for index, group in enumerate(groups):
        cells = set(group)
        group_cells.append(cells)
        for cell in group:
            cell_groups.setdefault(cell, set()).add(index)
    if cell_groups.get(start) or cell_groups.get(goal):
        return None, {'kind': 'endpoint_in_failure_group'}
    flow = _max_disjoint(passable, start, goal)
    if flow < 2:
        return None, {'kind': 'max_flow', 'value': flow}
    for index, cells in enumerate(group_cells):
        if not _reachable(passable, cells, start, goal):
            return None, {'kind': 'group_cut', 'group': index}
    lower = _lower_bounds(passable, cost, goal)
    cheapest = lower.get(start, math.inf)
    if cheapest > max_cost:
        return None, {'kind': 'budget', 'optimistic_cost': cheapest}

    def candidates(cell, spent, visited):
        options = []
        for neighbor in _neighbors(cell):
            if neighbor not in passable or neighbor in visited:
                continue
            new_spent = spent + cost[neighbor]
            if new_spent + lower.get(neighbor, math.inf) > max_cost:
                continue
            detour = abs(neighbor[0] - goal[0]) + abs(neighbor[1] - goal[1])
            options.append((new_spent + lower.get(neighbor, 0.0), detour,
                            neighbor, new_spent))
        options.sort()
        return [(neighbor, new_spent) for _, _, neighbor, new_spent in options]

    evaluated = 0
    visited = {start}
    path = [start]
    stack = [(start, 0.0, iter(candidates(start, 0.0, visited)),
              frozenset(), frozenset())]
    while stack:
        cell, spent, options, claimed, claimed_cells = stack[-1]
        descended = False
        for neighbor, new_spent in options:
            if neighbor == goal:
                evaluated += 1
                first = path + [goal]
                banned = (set(first) | claimed_cells) - {start, goal}
                _, second = _shortest(passable, cost, start, goal, max_cost, banned)
                if (second is not None
                        and _verify_pair(first, second, start, goal, cost, groups, max_cost)):
                    return (first, second), None
                continue
            new_claimed = claimed | cell_groups.get(neighbor, frozenset())
            if new_claimed == claimed:
                new_claimed_cells = claimed_cells
            else:
                new_claimed_cells = claimed_cells | set().union(
                    *(group_cells[index] for index in new_claimed - claimed))
            visited.add(neighbor)
            if (_reachable(passable, visited, neighbor, goal)
                    and (not new_claimed_cells
                         or _reachable(passable, visited | new_claimed_cells,
                                       start, goal))):
                path.append(neighbor)
                stack.append((neighbor, new_spent,
                              iter(candidates(neighbor, new_spent, visited)),
                              new_claimed, new_claimed_cells))
                descended = True
                break
            visited.discard(neighbor)
        if not descended:
            stack.pop()
            visited.discard(path.pop())
    return None, {'kind': 'exhaustive_search', 'candidates': evaluated}


def _route_groups(path, groups):
    cells = set(path)
    return [index for index, group in enumerate(groups) if cells & set(group)]


def _route_evidence(path, info, groups, cost):
    return {'cells': [[row, col] for row, col in path],
            'cost': _path_cost(path, cost),
            'coverage': [info[cell]['coverage'] for cell in path],
            'groups': _route_groups(path, groups)}


def _group_hits(paths, groups):
    hits = {}
    for index, group in enumerate(groups):
        cells = set(group)
        hits[str(index)] = [route for route, path in enumerate(paths) if cells & set(path)]
    return hits


def _assess_single(info, known_cost, bound_cost, start, goal, max_cost, groups):
    total, route = _shortest(set(known_cost), known_cost, start, goal, max_cost)
    if route is not None:
        return {'status': 'viable', 'reason': 'ok',
                'routes': [_route_evidence(route, info, groups, known_cost)],
                'unknown_cells': [],
                'evidence': {'graph': 'known', 'cost': total,
                             'group_hits': _group_hits([route], groups)}}
    bound_total, bound_route = _shortest(set(bound_cost), bound_cost, start, goal, math.inf)
    if bound_route is None:
        return {'status': 'blocked', 'reason': 'disconnected', 'routes': [],
                'unknown_cells': [],
                'evidence': {'graph': 'upper_bound', 'proof': {'kind': 'disconnected'}}}
    if bound_total > max_cost:
        return {'status': 'blocked', 'reason': 'budget_exceeded', 'routes': [],
                'unknown_cells': [],
                'evidence': {'graph': 'upper_bound',
                             'proof': {'kind': 'budget', 'optimistic_cost': bound_total}}}
    unknown = sorted(cell for cell in bound_route if info[cell]['state'] == UNKNOWN)
    return {'status': 'indeterminate', 'reason': 'unknown_cells', 'routes': [],
            'unknown_cells': [[row, col] for row, col in unknown],
            'evidence': {'graph': 'upper_bound', 'upper_bound_cost': bound_total,
                         'upper_bound_routes': [_route_evidence(bound_route, info, groups, bound_cost)],
                         'note': 'upper bound treats unknown cells as free; not a usable route'}}


def _assess_pair(info, known_cost, bound_cost, start, goal, max_cost, groups):
    pair, _ = _find_survivable_pair(set(known_cost), known_cost, start, goal, groups, max_cost)
    if pair is not None:
        return {'status': 'viable', 'reason': 'ok',
                'routes': [_route_evidence(path, info, groups, known_cost) for path in pair],
                'unknown_cells': [],
                'evidence': {'graph': 'known',
                             'costs': [_path_cost(path, known_cost) for path in pair],
                             'group_hits': _group_hits(pair, groups)}}
    bound_pair, proof = _find_survivable_pair(set(bound_cost), bound_cost,
                                              start, goal, groups, max_cost)
    if bound_pair is None:
        return {'status': 'blocked', 'reason': _BLOCKED_REASON[proof['kind']],
                'routes': [], 'unknown_cells': [],
                'evidence': {'graph': 'upper_bound', 'proof': proof}}
    unknown = sorted({cell for path in bound_pair for cell in path
                      if info[cell]['state'] == UNKNOWN})
    return {'status': 'indeterminate', 'reason': 'unknown_cells', 'routes': [],
            'unknown_cells': [[row, col] for row, col in unknown],
            'evidence': {'graph': 'upper_bound',
                         'upper_bound_routes': [_route_evidence(path, info, groups, bound_cost)
                                                for path in bound_pair],
                         'note': 'upper bound treats unknown cells as free; not usable routes'}}


def corridor_assessment(db_path, request_id, policy):
    """Evaluate a frozen corridor request under a route policy.

    Returns a three-state verdict: viable (certified routes in the known
    graph, with per-cell coverage and per-group intersections),
    blocked (impossible even in the optimistic upper-bound graph), or
    indeterminate (only the upper bound admits the policy; the unknown
    cells causing the difference are reported). Audit failure or an
    uninterpretable cost grid never yields viable. The conclusion is
    frozen with the run/contract/source/output hashes, the policy and
    the failure-group digest in `corridor_assessment`; repeated
    evaluation is idempotent and later tampering or re-runs recompute
    instead of reusing stale verdicts. `corridor_result` is untouched.
    """
    if policy not in POLICIES:
        raise ValueError('unknown_corridor_policy')
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        request = db.execute('SELECT * FROM corridor_request WHERE id=?', (request_id,)).fetchone()
        if not request:
            raise ValueError('unknown_corridor_request')
        run_id = request['run_id']
        run = db.execute('SELECT * FROM run WHERE id=?', (run_id,)).fetchone()
        if not run:
            raise ValueError('unknown_run')
        contract_row = db.execute('SELECT * FROM contract WHERE id=?', (run['contract_id'],)).fetchone()
        source_row = db.execute('SELECT * FROM source WHERE id=?', (run['source_id'],)).fetchone()
        output_row = db.execute('SELECT * FROM output WHERE run_id=?', (run_id,)).fetchone()
        contract = json.loads(contract_row['body'])
        raw_groups = json.loads(request['failure_groups'])
        groups = _normalize_groups(raw_groups)
        digest = _groups_digest(raw_groups)
        source_hash = _file_sha(source_row['path'])
        output_hash = _file_sha(output_row['path']) if output_row else 'missing'
        fingerprint = sha(json.dumps({
            'request': [run_id, request['start_cell'], request['goal_cell'],
                        request['max_cost'], request['failure_groups']],
            'policy': policy, 'contract_hash': contract_row['hash'],
            'source_hash': source_hash, 'output_hash': output_hash},
            sort_keys=True).encode())
        stored = db.execute('SELECT fingerprint,evidence FROM corridor_assessment '
                            'WHERE request_id=? AND policy=?', (request_id, policy)).fetchone()
        if stored and stored['fingerprint'] == fingerprint:
            result = json.loads(stored['evidence'])
            result['cached'] = True
            return result
        issues = audit(db_path, run_id) if output_row else ['output_missing']
        interpretable = (contract.get('kind') == 'continuous'
                         and bool(str(source_row['unit'] or '').strip()))
        if issues:
            result = {'status': 'indeterminate', 'reason': 'audit_failed',
                      'routes': [], 'unknown_cells': [],
                      'evidence': {'audit_issues': issues}}
        elif not interpretable:
            result = {'status': 'indeterminate', 'reason': 'uninterpretable_cost_grid',
                      'routes': [], 'unknown_cells': [],
                      'evidence': {'kind': contract.get('kind'), 'unit': source_row['unit']}}
        else:
            start = tuple(json.loads(request['start_cell']))
            goal = tuple(json.loads(request['goal_cell']))
            max_cost = float(request['max_cost'])
            info = _classify_cells(db, run_id, contract)
            known_cost = {cell: x['value'] for cell, x in info.items() if x['state'] == KNOWN}
            bound_cost = {cell: (x['value'] if x['state'] == KNOWN else 0.0)
                          for cell, x in info.items() if x['state'] != OBSTACLE}
            if policy == 'single_fast':
                result = _assess_single(info, known_cost, bound_cost, start, goal, max_cost, groups)
            else:
                result = _assess_pair(info, known_cost, bound_cost, start, goal, max_cost, groups)
        result['request_id'] = request_id
        result['policy'] = policy
        with db:
            db.execute('INSERT INTO corridor_assessment VALUES (?,?,?,?,?,?,?,?,?,?,?) '
                       'ON CONFLICT(request_id,policy) DO UPDATE SET '
                       'run_id=excluded.run_id,contract_hash=excluded.contract_hash,'
                       'source_hash=excluded.source_hash,output_hash=excluded.output_hash,'
                       'groups_digest=excluded.groups_digest,fingerprint=excluded.fingerprint,'
                       'status=excluded.status,reason=excluded.reason,evidence=excluded.evidence',
                       (request_id, policy, run_id, contract_row['hash'], source_hash,
                        output_hash, digest, fingerprint, result['status'], result['reason'],
                        json.dumps(result, sort_keys=True)))
        result['cached'] = False
        return result
    finally:
        db.close()
