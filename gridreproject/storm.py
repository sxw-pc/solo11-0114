"""Freeze local storm and outlet contracts for a previously audited terrain run."""
import hashlib
import json
from pathlib import Path

from .store import connect


def register(path, storm_id, terrain_run, event_file):
    if not storm_id:
        raise ValueError('empty_storm_id')
    event_file = Path(event_file).resolve()
    raw = event_file.read_bytes()
    event = json.loads(raw)
    if (not isinstance(event.get('rainfall_mm'), (int, float)) or
            event['rainfall_mm'] < 0 or
            not isinstance(event.get('outlets'), list) or not event['outlets']):
        raise ValueError('bad_storm_contract')
    if len({x['id'] for x in event['outlets']}) != len(event['outlets']):
        raise ValueError('duplicate_outlet')
    if any(x.get('warning_m3', -1) < 0 for x in event['outlets']):
        raise ValueError('bad_outlet_threshold')
    db = connect(path)
    try:
        with db:
            run = db.execute('SELECT id FROM run WHERE id=?', (terrain_run,)).fetchone()
            if not run:
                raise ValueError('unknown_terrain_run')
            cells = {(r['row'], r['col']) for r in db.execute(
                'SELECT row,col FROM cell WHERE run_id=?', (terrain_run,))}
            for outlet in event['outlets']:
                if (outlet['row'], outlet['col']) not in cells:
                    raise ValueError('outlet_outside_terrain')
            digest = hashlib.sha256(raw).hexdigest()
            payload = json.dumps({'file': str(event_file), 'digest': digest,
                                  'run': terrain_run, 'event': event},
                                 sort_keys=True, separators=(',', ':'))
            old = db.execute('SELECT payload FROM storm_contract WHERE id=?',
                             (storm_id,)).fetchone()
            if old and old['payload'] != payload:
                raise ValueError('storm_identity_conflict')
            db.execute('INSERT OR IGNORE INTO storm_contract VALUES (?,?)',
                       (storm_id, payload))
    finally:
        db.close()
    return {'storm_id': storm_id, 'event_digest': digest}
