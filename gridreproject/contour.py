"""Freeze an isoline request and expose unambiguous local crossings."""
import json
import math
import rasterio
from .store import connect
from .engine import audit

SCHEMA = """CREATE TABLE IF NOT EXISTS contour_request(
 id TEXT PRIMARY KEY, run_id INTEGER NOT NULL, level REAL NOT NULL,
 connectivity TEXT NOT NULL);
"""


def register(db_path, request_id, run_id, level, connectivity):
    if not request_id or not math.isfinite(level) or connectivity not in ('low_connected', 'high_connected'):
        raise ValueError('invalid_contour_request')
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        if not db.execute('SELECT 1 FROM run WHERE id=?', (run_id,)).fetchone():
            raise ValueError('unknown_run')
        with db:
            old = db.execute('SELECT run_id,level,connectivity FROM contour_request WHERE id=?',
                             (request_id,)).fetchone()
            if old and tuple(old) != (run_id, level, connectivity):
                raise ValueError('contour_request_conflict')
            db.execute('INSERT OR IGNORE INTO contour_request VALUES (?,?,?,?)',
                       (request_id, run_id, level, connectivity))
        return {'id': request_id}
    finally:
        db.close()


def segments(db_path, request_id):
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        row = db.execute('SELECT * FROM contour_request WHERE id=?', (request_id,)).fetchone()
        if not row:
            raise ValueError('unknown_contour_request')
        if audit(db_path, row['run_id']):
            raise ValueError('run_audit_failed')
        output = db.execute('SELECT path FROM output WHERE run_id=?', (row['run_id'],)).fetchone()
        with rasterio.open(output['path']) as dataset:
            values = dataset.read(1)
            nodata = dataset.nodata
        found = []
        unresolved = 0
        level = row['level']
        for r in range(values.shape[0]-1):
            for c in range(values.shape[1]-1):
                corners = [(r+.5,c+.5), (r+.5,c+1.5),
                           (r+1.5,c+1.5), (r+1.5,c+.5)]
                heights = [float(values[r,c]), float(values[r,c+1]),
                           float(values[r+1,c+1]), float(values[r+1,c])]
                if any(not math.isfinite(v) or v == nodata for v in heights):
                    continue
                crossings = []
                for i in range(4):
                    j = (i+1) % 4
                    a,b = heights[i],heights[j]
                    if (a < level) == (b < level):
                        continue
                    fraction = (level-a)/(b-a)
                    crossings.append([corners[i][0]+fraction*(corners[j][0]-corners[i][0]),
                                      corners[i][1]+fraction*(corners[j][1]-corners[i][1])])
                if len(crossings) == 2:
                    found.append({'square': [r,c], 'ends': crossings})
                elif len(crossings) == 4:
                    unresolved += 1
        return {'segments': found, 'unresolved_saddles': unresolved}
    finally:
        db.close()
