"""Zonal risk and product release decisions over a reprojection run.

Two interval-aware zone strategies are supported on top of a run that declares
a source error model:

``mean_bound``
    Average the per-cell lower and upper bounds over every valid cell in the
    zone. The averaged interval is compared with ``limit_value``.

``hotspot_bound``
    Take the most dangerous cell envelope: the zone statistic is
    ``[max(lower), max(upper)]``. It is safe only when every cell's upper bound
    is at or below the limit, and exceeded as soon as any cell's lower bound is
    above the limit.

Both strategies return 证据不足 whenever any required cell interval is unknown;
a nominal value that happens to look normal is never enough. Zones registered
without a strategy keep the original nominal-mean behaviour for runs that did
not declare an error model.
"""

import json

from .store import connect
from .engine import audit, canon

STRATEGIES = {'mean_bound', 'hotspot_bound'}


def register_zone(path, name, cells, min_coverage, limit_value,
                  strategy=None, strategy_version=None):
    if not name or not cells or not 0 < min_coverage <= 1:
        raise ValueError('bad_zone_contract')
    keys = [(int(r), int(c)) for r, c in cells]
    if len(set(keys)) != len(keys):
        raise ValueError('duplicate_zone_cell')
    if strategy is None:
        if strategy_version is not None:
            raise ValueError('bad_zone_strategy')
    else:
        if strategy not in STRATEGIES or not isinstance(strategy_version, str) or \
                not strategy_version.strip():
            raise ValueError('bad_zone_strategy')
    body = json.dumps(keys, sort_keys=True)
    db = connect(path)
    try:
        with db:
            old = db.execute('SELECT * FROM zone WHERE id=?', (name,)).fetchone()
            if old and (old['cell_ids'], old['min_coverage'], old['limit_value'],
                        old['strategy'], old['strategy_version']) != \
                    (body, min_coverage, limit_value, strategy, strategy_version):
                raise ValueError('zone_version_conflict')
            db.execute('INSERT OR IGNORE INTO zone VALUES (?,?,?,?,?,?)',
                       (name, body, min_coverage, limit_value, strategy, strategy_version))
    finally:
        db.close()


def _cell_evidence(db, run_id, r, c, missing_ok=False):
    row = db.execute('SELECT * FROM cell WHERE run_id=? AND row=? AND col=?',
                     (run_id, r, c)).fetchone()
    if row is None:
        if missing_ok:
            return None
        raise ValueError('zone_cell_outside_grid')
    return row


def _evaluate_zone(db, run_id, zone):
    ids = json.loads(zone['cell_ids'])
    run = db.execute('SELECT * FROM run WHERE id=?', (run_id,)).fetchone()
    rows = [_cell_evidence(db, run_id, r, c) for r, c in ids]
    covers = [row['coverage'] for row in rows]
    coverage = sum(min(max(x, 0), 1) for x in covers) / len(ids)
    values = [row['value'] for row in rows]

    strategy = zone['strategy']
    policy = {'strategy': strategy, 'strategy_version': zone['strategy_version'],
              'limit_value': zone['limit_value'], 'min_coverage': zone['min_coverage'],
              'cells': ids, 'model_version': run['error_model_version']}

    if strategy is None:
        # Original behaviour: a nominal unweighted mean, unchanged for runs
        # that do not declare an error model. A run that does declare one must
        # not be released through a strategy that ignores its intervals.
        measured = sum(v for v in values if v is not None) / \
            len([v for v in values if v is not None]) if any(v is not None for v in values) else None
        if run['error_model'] is not None:
            status = '证据不足'
        elif (run['status'] != '已核验' or coverage < zone['min_coverage'] or
                any(v is None for v in values)):
            status = '证据不足'
        elif measured > zone['limit_value']:
            status = '超限'
        else:
            status = '正常'
        evidence = [{'row': r, 'col': c, 'value': row['value'],
                     'coverage': row['coverage'], 'lower': row['value_lower'],
                     'upper': row['value_upper'], 'error_known': row['error_known']}
                    for (r, c), row in zip(ids, rows)]
        return {'status': status, 'measured': measured, 'coverage': coverage,
                'lower': None, 'upper': None, 'evidence': evidence, 'policy': policy,
                'model_version': run['error_model_version'],
                'model_hash': run['error_hash']}

    if strategy not in STRATEGIES:
        raise ValueError('bad_zone_strategy')

    # Interval-aware strategies need a declared error model on the run.
    intervals_ok = run['error_model'] is not None
    evidence = []
    lower_values = []
    upper_values = []
    for (r, c), row in zip(ids, rows):
        known = (row['error_known'] == 1 and row['value_lower'] is not None and
                 row['value_upper'] is not None)
        intervals_ok = intervals_ok and row['value'] is not None and known
        if known:
            lower_values.append(float(row['value_lower']))
            upper_values.append(float(row['value_upper']))
        evidence.append({
            'row': r, 'col': c, 'value': row['value'], 'coverage': row['coverage'],
            'lower': row['value_lower'], 'upper': row['value_upper'],
            'error_known': 1 if known else 0,
            'weight_evidence': json.loads(row['weight_evidence'])
            if row['weight_evidence'] else None,
            'limit_value': zone['limit_value'],
            'crosses_limit': bool(known and row['value_lower'] <= zone['limit_value']
                                  < row['value_upper']),
        })

    measured = None
    lower_stat = upper_stat = None
    if (run['status'] != '已核验' or coverage < zone['min_coverage'] or
            not intervals_ok or any(v is None for v in values)):
        status = '证据不足'
    else:
        if strategy == 'mean_bound':
            lower_stat = sum(lower_values) / len(lower_values)
            upper_stat = sum(upper_values) / len(upper_values)
            measured = sum(values) / len(values)
        else:
            lower_stat = max(lower_values)
            upper_stat = max(upper_values)
            measured = max(values)
        if upper_stat <= zone['limit_value']:
            status = '正常'
        elif lower_stat > zone['limit_value']:
            status = '超限'
        else:
            status = '证据不足'

    # Keep statistics visible even when evidence is missing, but only if all
    # required intervals are actually known.
    if lower_values and len(lower_values) == len(ids):
        if strategy == 'mean_bound':
            lower_stat = sum(lower_values) / len(lower_values)
            upper_stat = sum(upper_values) / len(upper_values)
        else:
            lower_stat = max(lower_values)
            upper_stat = max(upper_values)
    return {'status': status, 'measured': measured, 'coverage': coverage,
            'lower': lower_stat, 'upper': upper_stat, 'evidence': evidence,
            'policy': policy, 'model_version': run['error_model_version'],
            'model_hash': run['error_hash']}


def evaluate_zone(path, run_id, name):
    db = connect(path)
    try:
        zone = db.execute('SELECT * FROM zone WHERE id=?', (name,)).fetchone()
        run = db.execute('SELECT status FROM run WHERE id=?', (run_id,)).fetchone()
        if not zone or not run:
            raise ValueError('unknown_zone_or_run')
        # Re-verify the run before a zone decision so a failed audit can never
        # leave an old 正常 verdict usable for release.
        audit(path, run_id)
        result = _evaluate_zone(db, run_id, zone)
        db.execute(
            'INSERT INTO zone_decision(run_id,zone_id,status,measured,coverage,evidence,'
            'strategy,strategy_version,lower_bound,upper_bound,model_version,model_hash) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(run_id,zone_id) DO UPDATE SET status=excluded.status,'
            'measured=excluded.measured,coverage=excluded.coverage,'
            'evidence=excluded.evidence,strategy=excluded.strategy,'
            'strategy_version=excluded.strategy_version,'
            'lower_bound=excluded.lower_bound,upper_bound=excluded.upper_bound,'
            'model_version=excluded.model_version,model_hash=excluded.model_hash',
            (run_id, name, result['status'], result['measured'], result['coverage'],
             json.dumps(result['evidence'], ensure_ascii=False), result['policy']['strategy'],
             result['policy']['strategy_version'], result['lower'], result['upper'],
             result['model_version'], result['model_hash']))
        db.commit()
        return {'status': result['status'], 'measured': result['measured'],
                'coverage': result['coverage'], 'lower': result['lower'],
                'upper': result['upper']}
    finally:
        db.close()


def _release_policy(db, run_id, zones):
    run = db.execute('SELECT * FROM run WHERE id=?', (run_id,)).fetchone()
    policy = {'model_version': run['error_model_version'], 'zones': []}
    for name in zones:
        zone = db.execute('SELECT * FROM zone WHERE id=?', (name,)).fetchone()
        if not zone:
            raise ValueError('unknown_zone')
        policy['zones'].append({
            'zone': name,
            'strategy': zone['strategy'],
            'strategy_version': zone['strategy_version'],
            'limit_value': zone['limit_value'],
            'min_coverage': zone['min_coverage'],
            'cells': json.loads(zone['cell_ids']),
            'run_model_version': run['error_model_version'],
            'run_model_hash': run['error_hash'],
        })
    return canon(policy)


def decide_release(path, request_id, run_id, zones):
    if not zones or len(set(zones)) != len(zones):
        raise ValueError('bad_release_scope')
    # Re-evaluate the source, error grid and output before making the decision.
    audit(path, run_id)
    db = connect(path)
    try:
        run = db.execute('SELECT * FROM run WHERE id=?', (run_id,)).fetchone()
        if not run:
            raise ValueError('unknown_run')
        policy = _release_policy(db, run_id, zones)
        frozen = json.dumps(sorted(zones))
        old = db.execute('SELECT * FROM release_request WHERE id=?', (request_id,)).fetchone()
        if old:
            # A changed risk policy needs a new request; never silently rewrite
            # the frozen identity of an existing product decision.
            if (old['run_id'], old['zone_ids']) != (run_id, frozen):
                raise ValueError('release_identity_conflict')
            if old['policy'] is not None and old['policy'] != policy:
                raise ValueError('release_identity_conflict')
            if old['policy'] is None:
                # A pre-interval request only has the legacy nominal meaning.
                # Moving it onto an interval policy is a policy change too.
                legacy_only = (run['error_model'] is None and
                               all(z['strategy'] is None for z in
                                   json.loads(policy)['zones']))
                if not legacy_only:
                    raise ValueError('release_identity_conflict')
        decisions = []
        rows = []
        strategy_gap = False
        for name in zones:
            row = db.execute('SELECT * FROM zone_decision WHERE run_id=? AND zone_id=?',
                             (run_id, name)).fetchone()
            rows.append(row)
            decisions.append(row['status'] if row else '未裁定')
            zone = db.execute('SELECT strategy FROM zone WHERE id=?', (name,)).fetchone()
            # A run with declared measurement error can never be released on
            # the legacy nominal-mean strategy, even if an old decision says 正常.
            if run['error_model'] is not None and (zone is None or zone['strategy'] is None):
                strategy_gap = True
        known = [r for r in rows if r is not None]
        lower_bound = None
        upper_bound = None
        if len(known) == len(zones) and known and \
                all(r['lower_bound'] is not None and r['upper_bound'] is not None
                    for r in known):
            # Conservative envelope across every zone in the release scope.
            lower_bound = min(r['lower_bound'] for r in known)
            upper_bound = max(r['upper_bound'] for r in known)
        if run['status'] != '已核验' or strategy_gap or \
                any(x in {'证据不足', '未裁定'} for x in decisions):
            status, reason = '暂缓', 'evidence_gap'
        elif any(x == '超限' for x in decisions):
            status, reason = '拒发', 'risk_exceeded'
        else:
            status, reason = '准予发布', 'all_zones_safe'
        db.execute(
            'INSERT INTO release_request(id,run_id,zone_ids,status,reason,policy,lower_bound,'
            'upper_bound) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
            'status=excluded.status,reason=excluded.reason,lower_bound=excluded.lower_bound,'
            'upper_bound=excluded.upper_bound',
            (request_id, run_id, frozen, status, reason, policy, lower_bound, upper_bound))
        db.commit()
        return {'status': status, 'reason': reason, 'zone_decisions': decisions,
                'lower': lower_bound, 'upper': upper_bound}
    finally:
        db.close()
