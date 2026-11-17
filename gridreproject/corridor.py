"""Freeze a traversability corridor request and find one shortest route."""
import heapq
import json
from .store import connect
from .engine import audit

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
