"""Compare audited spatial runs using both average change and local hot spots."""
import json
from .store import connect
from .engine import audit, trusted, coverage, cell_area, sha

MIN_SERIES_RUNS = 3
MAX_SERIES_RUNS = 12
MIN_EVENT_OCCURRENCES = 3
PERSISTENCE_MODES = ('consecutive', 'cumulative')


def register(path, trend_id, baseline_run, target_run, zone_id, max_mean_increase,
             max_cell_increase, max_hotspot_fraction):
    if (not trend_id or baseline_run == target_run or max_mean_increase < 0 or
            max_cell_increase < 0 or not 0 <= max_hotspot_fraction <= 1):
        raise ValueError('invalid_trend_policy')
    db = connect(path)
    try:
        with db:
            for rid in (baseline_run, target_run):
                if not db.execute('SELECT 1 FROM run WHERE id=?', (rid,)).fetchone():
                    raise ValueError('unknown_run')
            if not db.execute('SELECT 1 FROM zone WHERE id=?', (zone_id,)).fetchone():
                raise ValueError('unknown_zone')
            old = db.execute('SELECT * FROM trend WHERE id=?', (trend_id,)).fetchone()
            policy = (baseline_run, target_run, zone_id, max_mean_increase,
                      max_cell_increase, max_hotspot_fraction)
            if old and tuple(old[k] for k in ('baseline_run', 'target_run', 'zone_id',
                                              'max_mean_increase', 'max_cell_increase',
                                              'max_hotspot_fraction')) != policy:
                raise ValueError('trend_identity_conflict')
            db.execute("INSERT OR IGNORE INTO trend VALUES (?,?,?,?,?,?,?,'未评估','not_evaluated')",
                       (trend_id, *policy))
    finally:
        db.close()


def evaluate(path, trend_id):
    db = connect(path)
    try:
        policy = db.execute('SELECT * FROM trend WHERE id=?', (trend_id,)).fetchone()
        if not policy:
            raise ValueError('unknown_trend')
        baseline, target = policy['baseline_run'], policy['target_run']
        db.close()
        audit(path, baseline)
        audit(path, target)
        db = connect(path)
        base = db.execute('SELECT status,contract_id FROM run WHERE id=?', (baseline,)).fetchone()
        later = db.execute('SELECT status,contract_id FROM run WHERE id=?', (target,)).fetchone()
        zone = db.execute('SELECT * FROM zone WHERE id=?', (policy['zone_id'],)).fetchone()
        cells = []
        for row, col in json.loads(zone['cell_ids']):
            a = db.execute('SELECT value,coverage FROM cell WHERE run_id=? AND row=? AND col=?',
                           (baseline, row, col)).fetchone()
            b = db.execute('SELECT value,coverage FROM cell WHERE run_id=? AND row=? AND col=?',
                           (target, row, col)).fetchone()
            if not a or not b:
                cells.append((row, col, None, None, None, 0.0, 0))
                continue
            coverage = min(a['coverage'], b['coverage'])
            valid = (a['value'] is not None and b['value'] is not None and
                     coverage >= zone['min_coverage'])
            delta = b['value'] - a['value'] if valid else None
            hotspot = int(delta is not None and delta > policy['max_cell_increase'])
            cells.append((row, col, a['value'], b['value'], delta, coverage, hotspot))
        if base['status'] != '已核验' or later['status'] != '已核验' or base['contract_id'] != later['contract_id']:
            status, reason = '不可比较', 'audit_or_grid_mismatch'
        elif any(x[4] is None for x in cells):
            status, reason = '不可比较', 'coverage_gap'
        else:
            mean_delta = sum(x[4] for x in cells) / len(cells)
            hotspot_fraction = sum(x[6] for x in cells) / len(cells)
            if mean_delta > policy['max_mean_increase'] or hotspot_fraction > policy['max_hotspot_fraction']:
                status, reason = '恶化', 'mean_or_hotspot_exceeded'
            elif mean_delta < -policy['max_mean_increase']:
                status, reason = '改善', 'mean_decrease'
            else:
                status, reason = '稳定', 'within_change_budget'
        with db:
            db.execute('DELETE FROM trend_cell WHERE trend_id=?', (trend_id,))
            for cell in cells:
                db.execute('INSERT INTO trend_cell VALUES (?,?,?,?,?,?,?,?)', (trend_id, *cell))
            db.execute('UPDATE trend SET status=?,reason=? WHERE id=?', (status, reason, trend_id))
        return {'status': status, 'reason': reason, 'cells': len(cells),
                'hotspots': sum(x[6] for x in cells)}
    finally:
        db.close()
