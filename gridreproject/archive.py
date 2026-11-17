"""Freeze an archive request and copy its raster files into a bundle."""
import hashlib
import json
import shutil
from pathlib import Path
from .store import connect
from .engine import audit

SCHEMA = """CREATE TABLE IF NOT EXISTS archive_request(
 id TEXT PRIMARY KEY, run_ids TEXT NOT NULL, policy TEXT NOT NULL,
 request_sha TEXT NOT NULL);
"""


def request(db_path, archive_id, run_ids, policy):
    if (not archive_id or not isinstance(run_ids, list) or not run_ids or
            len(set(run_ids)) != len(run_ids) or
            any(not isinstance(x, int) or isinstance(x, bool) or x <= 0 for x in run_ids) or
            policy not in ('product_only', 'full_replay')):
        raise ValueError('invalid_archive_request')
    run_ids = sorted(run_ids)
    body = json.dumps({'id': archive_id, 'run_ids': run_ids, 'policy': policy},
                      sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(body.encode()).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        for rid in run_ids:
            if not db.execute('SELECT 1 FROM run WHERE id=?', (rid,)).fetchone():
                raise ValueError('unknown_run')
        with db:
            old = db.execute('SELECT request_sha FROM archive_request WHERE id=?', (archive_id,)).fetchone()
            if old and old['request_sha'] != digest:
                raise ValueError('archive_request_conflict')
            db.execute('INSERT OR IGNORE INTO archive_request VALUES (?,?,?,?)',
                       (archive_id, json.dumps(run_ids), policy, digest))
        return {'id': archive_id, 'request_sha': digest}
    finally:
        db.close()


def export(db_path, archive_id, directory):
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        request_row = db.execute('SELECT * FROM archive_request WHERE id=?', (archive_id,)).fetchone()
        if not request_row:
            raise ValueError('unknown_archive_request')
        directory = Path(directory)
        if directory.exists():
            raise ValueError('archive_exists')
        directory.mkdir(parents=True)
        manifest = {'id': archive_id, 'policy': request_row['policy'], 'runs': []}
        for rid in json.loads(request_row['run_ids']):
            if audit(db_path, rid):
                raise ValueError('run_audit_failed')
            row = db.execute('''SELECT s.path AS source_path,s.hash AS source_hash,
                o.path AS output_path,o.hash AS output_hash,c.hash AS contract_hash
                FROM run r JOIN source s ON s.id=r.source_id
                JOIN output o ON o.run_id=r.id JOIN contract c ON c.id=r.contract_id
                WHERE r.id=?''', (rid,)).fetchone()
            if not row:
                raise ValueError('missing_archive_data')
            entry = {'run_id': rid, 'contract_hash': row['contract_hash'],
                     'output_hash': row['output_hash'], 'source_hash': row['source_hash']}
            output_name = f'run-{rid}-output.tif'
            shutil.copyfile(row['output_path'], directory / output_name)
            entry['output_file'] = output_name
            if request_row['policy'] == 'full_replay':
                source_name = f'run-{rid}-source.tif'
                shutil.copyfile(row['source_path'], directory / source_name)
                entry['source_file'] = source_name
            manifest['runs'].append(entry)
        (directory / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True), encoding='utf-8')
        return manifest
    finally:
        db.close()
