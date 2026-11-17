"""Register surveyed observation sites against a frozen terrain run."""
import hashlib
import json
from pathlib import Path

from .store import connect


def register(path, network_id, terrain_run, sites_file):
    if not network_id:
        raise ValueError('empty_network_id')
    sites_file = Path(sites_file).resolve()
    raw = sites_file.read_bytes()
    sites = json.loads(raw)
    if not isinstance(sites, list) or not sites:
        raise ValueError('empty_sites')
    if len({s['id'] for s in sites}) != len(sites):
        raise ValueError('duplicate_site')
    db = connect(path)
    try:
        with db:
            row = db.execute('''SELECT contract.body FROM run JOIN contract
                    ON contract.id=run.contract_id WHERE run.id=?''',
                             (terrain_run,)).fetchone()
            if not row:
                raise ValueError('unknown_terrain_run')
            contract = json.loads(row['body'])
            x0, y0, x1, y1 = contract['bounds']
            for site in sites:
                if (not x0 <= site['x'] < x1 or not y0 <= site['y'] < y1 or
                        site['antenna_m'] < 0 or site['max_range_m'] <= 0 or
                        site['min_clearance_m'] < 0):
                    raise ValueError('invalid_survey_site')
            digest = hashlib.sha256(raw).hexdigest()
            payload = json.dumps({'run': terrain_run, 'file': str(sites_file),
                                  'digest': digest, 'sites': sites},
                                 sort_keys=True, separators=(',', ':'))
            old = db.execute('SELECT payload FROM sight_network WHERE id=?',
                             (network_id,)).fetchone()
            if old and old['payload'] != payload:
                raise ValueError('sight_network_conflict')
            db.execute('INSERT OR IGNORE INTO sight_network VALUES (?,?)',
                       (network_id, payload))
    finally:
        db.close()
    return {'network_id': network_id, 'site_count': len(sites), 'digest': digest}
