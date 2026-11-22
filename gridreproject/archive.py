"""Freeze an archive request and copy its raster files into a bundle.

Two archive generations coexist:

* :func:`request` / :func:`export` are the frozen legacy flow: a request
  fixes the run id set and the ``product_only`` / ``full_replay`` purpose,
  then each run is copied into a per-run directory. Their observable
  behaviour must not change.
* :func:`publish` / :func:`verify_bundle` / :func:`restore` implement
  content-addressed, deduplicated, atomically sealed bundles with honest,
  policy-specific offline recovery guarantees.
"""
import gzip
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import rasterio
import numpy as np

from .store import connect, frozen_view
from .engine import audit, audit_snapshot, sha

ARCHIVE_FORMAT = 'gridreproject-archive/1'
OBJECTS_DIRNAME = 'objects'
MANIFEST_NAME = 'manifest.json'
STAGING_PREFIX = '.staging-'

# Guarantees printed on the bundle itself.  ``display_only`` never claims
# source/cell re-verification; ``full_replay`` promises an independent audit
# on a brand new ledger and file tree.
GUARANTEE_DISPLAY = 'display_only'
GUARANTEE_REPLAY = 'full_replay_audited'


class ArchiveError(ValueError):
    """A seal/verify/restore failure.

    The message is always a stable machine-readable diagnostic code so callers
    can distinguish tampering, budget and conflict failures programmatically.
    """


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode('utf-8')


# ---------------------------------------------------------------------------
# Frozen legacy flow (behaviour intentionally unchanged)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Snapshot + content-addressed object storage
# ---------------------------------------------------------------------------
def _snapshot_run(db, rid):
    """Collect every ledger row a run depends on from one frozen view."""
    row = db.execute('''SELECT r.id AS run_id,r.status AS run_status,r.method AS method,
        r.started AS started,r.ended AS ended,
        s.id AS source_row_id,s.path AS source_path,s.hash AS source_hash,
        s.width AS source_width,s.height AS source_height,s.crs AS source_crs,
        s.nodata AS source_nodata,s.unit AS source_unit,
        c.id AS contract_row_id,c.hash AS contract_hash,c.body AS contract_body,
        o.path AS output_path,o.hash AS output_hash
        FROM run r JOIN source s ON s.id=r.source_id
        JOIN contract c ON c.id=r.contract_id
        JOIN output o ON o.run_id=r.id WHERE r.id=?''', (rid,)).fetchone()
    if not row:
        raise ArchiveError('missing_archive_data')
    cells = [dict(x) for x in db.execute(
        'SELECT row,col,value,coverage,source_row,source_col,weight FROM cell '
        'WHERE run_id=? ORDER BY row,col', (rid,))]
    if not cells:
        raise ArchiveError('cell_evidence_missing')
    issues = [dict(x) for x in db.execute(
        'SELECT row,col,code,detail FROM issue WHERE run_id=? ORDER BY row,col', (rid,))]
    return dict(row), cells, issues


def _read_object_bytes(path):
    """Indirection point: tests swap the bytes seen mid-copy to prove
    that copy-time re-hashing rejects a file replaced after the audit."""
    return Path(path).read_bytes()


def _store_object(staging_objects, expected_hash, source_path, copied, budget_state):
    """Copy one object into the staging store and re-hash the bytes itself.

    Content hash is the object identity: equal hashes share one byte store.
    Every byte is counted exactly once, no matter how many runs reference it.
    """
    if expected_hash in copied:
        return 0
    target = staging_objects / expected_hash
    data = _read_object_bytes(source_path)
    actual = sha(data)
    if actual != expected_hash:
        raise ArchiveError('object_changed_after_audit:' + expected_hash)
    tmp = target.with_name(target.name + '.part')
    tmp.write_bytes(data)
    os.replace(tmp, target)
    copied.add(expected_hash)
    budget_state['object_bytes'] += len(data)
    return len(data)


def _object_record(digest, kind, size, encoding='identity'):
    return {'hash': digest, 'kind': kind, 'size': size, 'encoding': encoding}


def _seal_manifest(manifest):
    """Sign the manifest over its canonical form, excluding the seal itself."""
    body = {k: v for k, v in manifest.items() if k != 'seal'}
    return hashlib.sha256(_canonical(body)).hexdigest()


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------
def publish(request_id, archive_root, byte_budget, db_path=None):
    """Seal a deduplicated, self-verifying bundle for a frozen request.

    The bundle is assembled in a private staging directory and only renamed
    into place after every object, reference and the offline verification
    pass. Retrying the same request with the same budget is idempotent and
    never overwrites an already signed bundle. A budget shortfall raises
    :class:`ArchiveError` (``byte_budget_exceeded``) with a diagnostic and
    leaves no bundle or partial directory behind.

    ``db_path`` locates the run ledger; when omitted it is taken from the
    ``GRIDREPROJECT_ARCHIVE_DB`` environment variable or a ``ledger.db``
    sibling of ``archive_root``.
    """
    if not isinstance(request_id, str) or not request_id:
        raise ArchiveError('invalid_request_id')
    if not isinstance(byte_budget, int) or isinstance(byte_budget, bool) or byte_budget < 0:
        raise ArchiveError('invalid_byte_budget')
    root = Path(archive_root)
    if not root.is_dir():
        raise ArchiveError('archive_root_missing')

    # The request ledger lives next to the archive root by convention; tests
    # and callers with another layout pass it explicitly.
    if db_path is None:
        db_path = os.environ.get('GRIDREPROJECT_ARCHIVE_DB')
    if db_path is None:
        sibling = root.parent / 'ledger.db'
        db_path = sibling if sibling.is_file() else None
    if db_path is None or not Path(db_path).is_file():
        raise ArchiveError('ledger_not_found')

    final_dir = root / request_id
    if final_dir.exists():
        # Idempotent retry: an existing, valid, same-identity bundle is
        # returned as-is. A mismatching or corrupt one is never overwritten.
        existing = verify_bundle(final_dir)
        if existing['request_id'] != request_id:
            raise ArchiveError('bundle_identity_conflict')
        return existing['manifest']

    staging = root / (STAGING_PREFIX + request_id)
    if staging.exists():
        # Crash residue from a previous attempt is private and removable.
        shutil.rmtree(staging)
    (staging / OBJECTS_DIRNAME).mkdir(parents=True)

    try:
        with frozen_view(db_path) as db:
            db.executescript(SCHEMA)
            request_row = db.execute(
                'SELECT * FROM archive_request WHERE id=?', (request_id,)).fetchone()
            if not request_row:
                raise ArchiveError('unknown_archive_request')
            policy = request_row['policy']
            run_ids = json.loads(request_row['run_ids'])

            # Pre-flight audit inside the frozen view: no ledger mutation,
            # and every later read reuses this same consistent snapshot.
            for rid in run_ids:
                if audit_snapshot(db, rid):
                    raise ArchiveError('run_audit_failed:' + str(rid))

            objects = {}
            runs = []
            copied = set()
            budget_state = {'object_bytes': 0}

            def add_object(digest, kind, source_path, encoding='identity'):
                if digest not in objects:
                    size = _store_object(staging / OBJECTS_DIRNAME, digest,
                                         source_path, copied, budget_state)
                    objects[digest] = _object_record(digest, kind, size, encoding)
                return objects[digest]

            for rid in run_ids:
                snap, cells, issues = _snapshot_run(db, rid)
                if snap['run_status'] != '已核验':
                    raise ArchiveError('run_not_verified:' + str(rid))

                evidence = {
                    'run_id': rid,
                    'run': {
                        'id': rid, 'status': snap['run_status'], 'method': snap['method'],
                        'started': snap['started'], 'ended': snap['ended'],
                    },
                    'source': {
                        # Original path is intentionally absent: the hash is
                        # the identity and recovery must mint fresh paths.
                        'hash': snap['source_hash'], 'width': snap['source_width'],
                        'height': snap['source_height'], 'crs': snap['source_crs'],
                        'nodata': snap['source_nodata'], 'unit': snap['source_unit'],
                    },
                    'contract_hash': snap['contract_hash'],
                    'output_hash': snap['output_hash'],
                    'cells': cells,
                    'issues': issues,
                }
                evidence_bytes = gzip.compress(_canonical(evidence), compresslevel=6)
                evidence_hash = sha(evidence_bytes)
                ev_path = staging / OBJECTS_DIRNAME / (evidence_hash + '.part')
                ev_path.write_bytes(evidence_bytes)
                os.replace(ev_path, staging / OBJECTS_DIRNAME / evidence_hash)
                copied.add(evidence_hash)
                budget_state['object_bytes'] += len(evidence_bytes)
                objects[evidence_hash] = _object_record(
                    evidence_hash, 'evidence', len(evidence_bytes), 'gzip')

                add_object(snap['output_hash'], 'output_tiff', snap['output_path'])
                # Contract body is derived from the ledger, not from a path:
                # write it directly and verify its own hash.
                if snap['contract_hash'] not in objects:
                    body_bytes = snap['contract_body'].encode('utf-8')
                    if sha(body_bytes) != snap['contract_hash']:
                        raise ArchiveError('contract_hash_mismatch')
                    cpath = staging / OBJECTS_DIRNAME / snap['contract_hash']
                    cpath.write_bytes(body_bytes)
                    copied.add(snap['contract_hash'])
                    budget_state['object_bytes'] += len(body_bytes)
                    objects[snap['contract_hash']] = _object_record(
                        snap['contract_hash'], 'contract_body', len(body_bytes))

                run_ref = {
                    'run_id': rid,
                    'source_hash': snap['source_hash'],
                    'output_hash': snap['output_hash'],
                    'contract_hash': snap['contract_hash'],
                    'evidence_hash': evidence_hash,
                    'references': [],
                }
                if policy == 'full_replay':
                    add_object(snap['source_hash'], 'source_tiff', snap['source_path'])
                    run_ref['references'].append(
                        {'object': snap['source_hash'], 'role': 'source_tiff'})
                else:
                    # product_only keeps source metadata for traceability but
                    # must never imply the source bytes can be re-verified.
                    run_ref['references'].append(
                        {'object': None, 'role': 'source_metadata_only',
                         'source_hash': snap['source_hash']})
                run_ref['references'].extend([
                    {'object': snap['output_hash'], 'role': 'output_tiff'},
                    {'object': snap['contract_hash'], 'role': 'contract_body'},
                    {'object': evidence_hash, 'role': 'run_evidence'},
                ])
                runs.append(run_ref)

            guarantee = GUARANTEE_REPLAY if policy == 'full_replay' else GUARANTEE_DISPLAY
            manifest = {
                'format': ARCHIVE_FORMAT,
                'request_id': request_id,
                'request_sha': request_row['request_sha'],
                'policy': policy,
                'guarantee': guarantee,
                'run_ids': run_ids,
                'objects': [objects[h] for h in sorted(objects)],
                'runs': runs,
                'budget': {
                    'byte_budget': byte_budget,
                    'object_bytes': budget_state['object_bytes'],
                    'unique_object_count': len(objects),
                    'deduplicated_source_bytes': 0,
                },
            }
            # Account for shared-source savings explicitly so the budget
            # diagnostic can prove duplicate sources are never per-run charged.
            if policy == 'full_replay':
                source_hashes = [r['source_hash'] for r in runs]
                unique_sources = set(source_hashes)
                source_sizes = {
                    h: objects[h]['size'] for h in unique_sources
                    if h in objects and objects[h]['kind'] == 'source_tiff'}
                per_run = sum(source_sizes.get(h, 0) for h in source_hashes)
                manifest['budget']['deduplicated_source_bytes'] = (
                    per_run - sum(source_sizes.values()))

            # Final accounting first: refuse before the signed manifest is
            # ever written, so an over-budget attempt leaves no seal behind.
            # The manifest's own byte cost is estimated from its canonical
            # form and confirmed against the actual file afterwards.
            manifest['budget']['manifest_bytes_estimate'] = len(_canonical(manifest))
            projected = (budget_state['object_bytes']
                         + manifest['budget']['manifest_bytes_estimate'])
            if projected > byte_budget:
                raise ArchiveError(
                    'byte_budget_exceeded:needed=%d budget=%d unique_objects=%d'
                    % (projected, byte_budget, len(objects)))
            del manifest['budget']['manifest_bytes_estimate']

            manifest['seal'] = _seal_manifest(manifest)
            manifest_path = staging / MANIFEST_NAME
            manifest_path.write_bytes(_canonical(manifest))
            manifest_bytes = len(manifest_path.read_bytes())
            total = budget_state['object_bytes'] + manifest_bytes
            manifest['budget']['manifest_bytes'] = manifest_bytes
            manifest['budget']['total_bytes'] = total
            manifest['seal'] = _seal_manifest(manifest)
            manifest_path.write_bytes(_canonical(manifest))

            # Authoritative check on bytes actually written (the estimate and
            # the real manifest can only differ by the seal/accounting delta).
            if total > byte_budget:
                raise ArchiveError(
                    'byte_budget_exceeded:needed=%d budget=%d unique_objects=%d'
                    % (total, byte_budget, len(objects)))

        # Offline self-verification on the staged bytes before publication.
        report = verify_bundle(staging)
        if report['guarantee'] != guarantee:
            raise ArchiveError('staging_verification_failed')
        os.replace(staging, final_dir)
        return json.loads((final_dir / MANIFEST_NAME).read_text(encoding='utf-8'))
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# Offline verification
# ---------------------------------------------------------------------------
def _load_manifest(bundle_path):
    bundle = Path(bundle_path)
    manifest_path = bundle / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ArchiveError('manifest_missing')
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ArchiveError('manifest_unreadable') from exc
    if not isinstance(manifest, dict):
        raise ArchiveError('manifest_unreadable')
    if manifest.get('format') != ARCHIVE_FORMAT:
        raise ArchiveError('bundle_format_unknown')
    if manifest.get('policy') not in ('product_only', 'full_replay'):
        raise ArchiveError('manifest_policy_unknown')
    for field in ('request_id', 'run_ids', 'objects', 'runs', 'budget'):
        if field not in manifest:
            raise ArchiveError('manifest_field_missing:' + field)
    seal = manifest.get('seal')
    expected = _seal_manifest(manifest)
    if not seal or seal != expected:
        raise ArchiveError('seal_broken')
    return manifest


def _verify_structure(manifest, bundle):
    """Hash every object on disk and cross-check every run reference.

    A manifest-only summary is never trusted: each object is re-read and
    re-hashed, and every per-run reference must resolve to a matching object.
    """
    objects_dir = bundle / OBJECTS_DIRNAME
    if not objects_dir.is_dir():
        raise ArchiveError('objects_missing')
    by_hash = {o['hash']: o for o in manifest.get('objects', [])}
    if len(by_hash) != len(manifest.get('objects', [])):
        raise ArchiveError('duplicate_object_identity')
    for digest, record in by_hash.items():
        opath = objects_dir / digest
        if not opath.is_file():
            raise ArchiveError('object_missing:' + digest)
        actual = sha(opath.read_bytes())
        if actual != digest:
            raise ArchiveError('object_corrupt:' + digest)
        if opath.stat().st_size != record['size']:
            raise ArchiveError('object_size_mismatch:' + digest)

    run_ids = manifest.get('run_ids', [])
    if [r['run_id'] for r in manifest.get('runs', [])] != run_ids:
        raise ArchiveError('run_manifest_order_conflict')
    if len(set(run_ids)) != len(run_ids):
        raise ArchiveError('duplicate_run_reference')

    for ref in manifest['runs']:
        # product_only bundles deliberately omit source bytes: the source
        # hash stays on the run as traceable metadata, not an object reference.
        required = (ref['output_hash'], ref['contract_hash'], ref['evidence_hash'])
        for role_hash in required:
            if role_hash not in by_hash:
                raise ArchiveError(
                    'dangling_reference:run=%d object=%s' % (ref['run_id'], role_hash))
        if manifest['policy'] == 'full_replay' and ref['source_hash'] not in by_hash:
            raise ArchiveError(
                'dangling_reference:run=%d object=%s' % (ref['run_id'], ref['source_hash']))
        if by_hash[ref['output_hash']]['kind'] != 'output_tiff':
            raise ArchiveError('reference_kind_mismatch:output_tiff')
        if by_hash[ref['contract_hash']]['kind'] != 'contract_body':
            raise ArchiveError('reference_kind_mismatch:contract_body')
        if by_hash[ref['evidence_hash']]['kind'] != 'evidence':
            raise ArchiveError('reference_kind_mismatch:evidence')
        evidence = _verify_evidence_coverage(bundle, ref)
        if evidence['run_id'] != ref['run_id']:
            raise ArchiveError('evidence_identity_mismatch')
        if evidence['output_hash'] != ref['output_hash']:
            raise ArchiveError('evidence_output_mismatch')
        if evidence['contract_hash'] != ref['contract_hash']:
            raise ArchiveError('evidence_contract_mismatch')
    return by_hash


def _load_evidence(bundle, evidence_hash):
    raw = (bundle / OBJECTS_DIRNAME / evidence_hash).read_bytes()
    try:
        return json.loads(gzip.decompress(raw).decode('utf-8'))
    except (gzip.BadGzipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ArchiveError('evidence_unreadable:' + evidence_hash) from exc


def _verify_output_grid(bundle, obj_hash, contract_hash):
    """Open the stored TIFF without the original path and check its grid
    against the archived contract body — the display_only guarantee."""
    contract = json.loads(
        (bundle / OBJECTS_DIRNAME / contract_hash).read_bytes().decode('utf-8'))
    with rasterio.open(bundle / OBJECTS_DIRNAME / obj_hash) as src:
        if src.width != contract['width'] or src.height != contract['height']:
            raise ArchiveError('grid_mismatch')
        if str(src.crs) != str(rasterio.crs.CRS.from_string(contract['crs'])):
            raise ArchiveError('grid_mismatch')
        if not np.allclose(tuple(src.bounds), tuple(contract['bounds']),
                           rtol=0, atol=1e-8):
            raise ArchiveError('bounds_mismatch')


def _verify_evidence_coverage(bundle, ref):
    """The per-cell evidence must cover exactly the contract grid and each
    recorded cell must carry the provenance fields the engine persists."""
    evidence = _load_evidence(bundle, ref['evidence_hash'])
    contract = json.loads(
        (bundle / OBJECTS_DIRNAME / ref['contract_hash']).read_bytes().decode())
    cells = evidence.get('cells', [])
    expected = int(contract['width']) * int(contract['height'])
    if len(cells) != expected:
        raise ArchiveError('cell_evidence_mismatch')
    coords = {(c['row'], c['col']) for c in cells}
    if len(coords) != expected:
        raise ArchiveError('cell_evidence_duplicate')
    required = {'row', 'col', 'value', 'coverage', 'source_row', 'source_col', 'weight'}
    if any(required - set(c) for c in cells):
        raise ArchiveError('cell_evidence_field_missing')
    if evidence.get('source', {}).get('hash') != ref['source_hash']:
        raise ArchiveError('evidence_source_mismatch')
    return evidence


def verify_bundle(bundle_path):
    """Offline verification of a sealed bundle.

    Returns a report describing the honest guarantee; raises
    :class:`ArchiveError}` on any broken seal, corrupt/missing object,
    dangling reference or (for ``full_replay``) failed re-audit. Nothing
    here trusts the manifest's self-reported totals.
    """
    bundle = Path(bundle_path)
    manifest = _load_manifest(bundle)
    by_hash = _verify_structure(manifest, bundle)
    policy = manifest['policy']
    report = {
        'request_id': manifest['request_id'],
        'policy': policy,
        'guarantee': manifest['guarantee'],
        'runs': list(manifest['run_ids']),
        'manifest': manifest,
    }

    for ref in manifest['runs']:
        _verify_output_grid(bundle, ref['output_hash'], ref['contract_hash'])

    if policy == 'product_only':
        if manifest['guarantee'] != GUARANTEE_DISPLAY:
            raise ArchiveError('guarantee_label_mismatch')
        # No source_tiff objects may exist in a product_only bundle, so the
        # label can never be mistaken for a re-verifiable archive.
        if any(o['kind'] == 'source_tiff' for o in manifest['objects']):
            raise ArchiveError('product_only_contains_source_bytes')
        report['verified'] = ['output_openable', 'output_grid_and_hash',
                              'manifest_seal', 'object_hashes']
        report['not_claimed'] = ['source_reverification', 'cell_semantic_reaudit']
        return report

    if manifest['guarantee'] != GUARANTEE_REPLAY:
        raise ArchiveError('guarantee_label_mismatch')
    for ref in manifest['runs']:
        if by_hash[ref['source_hash']]['kind'] != 'source_tiff':
            raise ArchiveError('reference_kind_mismatch:source_tiff')

    # The strongest offline check: restore into a throwaway ledger/tree and
    # re-run the existing audit there, independent of original paths/db.
    with tempfile.TemporaryDirectory(prefix='gridreproject-verify-') as tmp:
        tmp = Path(tmp)
        restored = _restore_replay(bundle, manifest, tmp / 'ledger.db',
                                   tmp / 'files')
        for rid in manifest['run_ids']:
            new_rid = restored['run_id_map'][rid]
            issues = audit(restored['db_path'], new_rid)
            if issues:
                raise ArchiveError(
                    'restored_audit_failed:run=%d issues=%s' % (rid, ','.join(issues)))
    report['verified'] = ['manifest_seal', 'object_hashes', 'fresh_ledger_audit',
                          'source_contract_run_cell_output_chain']
    report['not_claimed'] = []
    return report


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
def _prepare_restore_targets(fresh_db, fresh_files):
    db_path = Path(fresh_db)
    files_dir = Path(fresh_files)
    for candidate in (db_path, files_dir):
        if candidate.exists():
            raise ArchiveError('restore_target_exists:' + str(candidate))
    files_dir.mkdir(parents=True)
    db = connect(db_path)
    db.close()
    return db_path, files_dir


def _restore_replay(bundle, manifest, fresh_db, fresh_files):
    """Rebuild source/contract/run/cell/issue/output rows on a fresh ledger.

    Every restored path is newly minted under ``fresh_files`` and every row
    gets fresh integer ids; the mapping back to archived ids is returned. The
    original database and original outputs are never touched.
    """
    db_path, files_dir = _prepare_restore_targets(fresh_db, fresh_files)
    db = connect(db_path)
    run_id_map = {}
    source_id_map = {}
    contract_id_map = {}
    output_path_map = {}
    try:
        with db:
            for ref in manifest['runs']:
                evidence = _load_evidence(bundle, ref['evidence_hash'])
                src_meta = evidence['source']
                src_hash = ref['source_hash']
                if src_hash not in source_id_map:
                    src_file = files_dir / ('source-%s.tif' % src_hash[:16])
                    shutil.copyfile(bundle / OBJECTS_DIRNAME / src_hash, src_file)
                    if sha(src_file.read_bytes()) != src_hash:
                        raise ArchiveError('restore_hash_mismatch:source')
                    cur = db.execute(
                        'INSERT INTO source(path,hash,width,height,crs,nodata,unit) '
                        'VALUES (?,?,?,?,?,?,?)',
                        (str(src_file), src_hash, src_meta['width'], src_meta['height'],
                         src_meta['crs'], src_meta['nodata'], src_meta['unit']))
                    source_id_map[src_hash] = cur.lastrowid

                if ref['contract_hash'] not in contract_id_map:
                    body = (bundle / OBJECTS_DIRNAME / ref['contract_hash']).read_bytes()
                    cur = db.execute('INSERT INTO contract(hash,body) VALUES (?,?)',
                                     (ref['contract_hash'], body.decode('utf-8')))
                    contract_id_map[ref['contract_hash']] = cur.lastrowid

                run_info = evidence['run']
                cur = db.execute(
                    'INSERT INTO run(source_id,contract_id,status,method,started,ended) '
                    'VALUES (?,?,?,?,?,?)',
                    (source_id_map[src_hash], contract_id_map[ref['contract_hash']],
                     run_info['status'], run_info['method'],
                     run_info.get('started'), run_info.get('ended')))
                new_rid = cur.lastrowid
                run_id_map[ref['run_id']] = new_rid

                db.executemany(
                    'INSERT INTO cell VALUES (?,?,?,?,?,?,?,?)',
                    [(new_rid, c['row'], c['col'], c['value'], c['coverage'],
                      c['source_row'], c['source_col'], c['weight'])
                     for c in evidence['cells']])
                db.executemany(
                    'INSERT INTO issue(run_id,row,col,code,detail) VALUES (?,?,?,?,?)',
                    [(new_rid, i.get('row'), i.get('col'), i['code'], i.get('detail'))
                     for i in evidence['issues']])

                out_file = files_dir / ('run-%d-output.tif' % new_rid)
                shutil.copyfile(bundle / OBJECTS_DIRNAME / ref['output_hash'], out_file)
                if sha(out_file.read_bytes()) != ref['output_hash']:
                    raise ArchiveError('restore_hash_mismatch:output')
                db.execute('INSERT INTO output VALUES (?,?,?)',
                           (new_rid, str(out_file), ref['output_hash']))
                output_path_map[ref['run_id']] = str(out_file)
    finally:
        db.close()
    return {'db_path': str(db_path), 'files_dir': str(files_dir),
            'run_id_map': run_id_map, 'source_id_map': source_id_map,
            'contract_id_map': contract_id_map, 'output_path_map': output_path_map}


def restore(bundle_path, fresh_db, fresh_files):
    """Restore a verified bundle into brand new paths and a fresh ledger.

    ``full_replay`` bundles rebuild the full evidence chain and are re-audited
    on the new SQLite database; ``product_only`` bundles only restore
    openable, grid/hash-conforming display TIFFs and explicitly carry the
    ``display_only`` guarantee — they never report a replay audit.
    """
    bundle = Path(bundle_path)
    report = verify_bundle(bundle)
    manifest = report['manifest']

    if manifest['policy'] == 'full_replay':
        restored = _restore_replay(bundle, manifest, fresh_db, fresh_files)
        for archived_rid in manifest['run_ids']:
            new_rid = restored['run_id_map'][archived_rid]
            issues = audit(restored['db_path'], new_rid)
            if issues:
                raise ArchiveError(
                    'restored_audit_failed:run=%d issues=%s'
                    % (archived_rid, ','.join(issues)))
        restored['guarantee'] = GUARANTEE_REPLAY
        restored['display_only'] = False
        restored['request_id'] = manifest['request_id']
        return restored

    # product_only: display outputs only, no source/cell re-verification.
    db_path, files_dir = _prepare_restore_targets(fresh_db, fresh_files)
    output_path_map = {}
    run_id_map = {}
    for surrogate_rid, ref in enumerate(manifest['runs'], start=1):
        # Surrogate ids are sequential and independent of archived integer
        # ids; no run rows are minted, so this must not read as ledger identity.
        run_id_map[ref['run_id']] = surrogate_rid
        out_file = files_dir / ('run-%d-display.tif' % surrogate_rid)
        shutil.copyfile(bundle / OBJECTS_DIRNAME / ref['output_hash'], out_file)
        if sha(out_file.read_bytes()) != ref['output_hash']:
            raise ArchiveError('restore_hash_mismatch:output')
        with rasterio.open(out_file) as src:
            if src.width <= 0 or src.height <= 0:
                raise ArchiveError('restored_output_unopenable')
        output_path_map[ref['run_id']] = str(out_file)
    return {
        'request_id': manifest['request_id'],
        'guarantee': GUARANTEE_DISPLAY,
        'display_only': True,
        'db_path': str(db_path),
        'files_dir': str(files_dir),
        'run_id_map': run_id_map,
        'output_path_map': output_path_map,
        'not_claimed': ['source_reverification', 'cell_semantic_reaudit'],
    }
