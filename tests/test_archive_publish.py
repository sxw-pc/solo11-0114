"""Offline tests for content-addressed archive sealing, verification and restore."""
import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import rasterio

from gridreproject import archive, engine
from gridreproject.store import connect
from archive_fixtures import (
    build_shared_source_scenario,
    dedup_budget,
    per_run_budget_floor,
)


@pytest.fixture()
def scenario(tmp_path):
    sc = build_shared_source_scenario(tmp_path)
    archive_root = tmp_path / 'archives'
    archive_root.mkdir()
    sc['archive_root'] = archive_root
    return sc


def _publish(sc, request_id, run_ids, policy, budget=None):
    if budget is None:
        budget = dedup_budget(sc)
    archive.request(sc['db_path'], request_id, run_ids, policy)
    return archive.publish(request_id, sc['archive_root'], budget,
                           db_path=sc['db_path'])


# ---------------------------------------------------------------------------
# Byte budget and shared-source deduplication
# ---------------------------------------------------------------------------
def test_dedup_budget_charges_shared_source_once(scenario):
    manifest = _publish(scenario, 'arc-replay', scenario['run_ids'], 'full_replay')
    budget = manifest['budget']
    source_size = scenario['source_size']
    outputs = sum(scenario['output_sizes'])

    # Exactly one source object exists even though two runs reference it.
    source_objects = [o for o in manifest['objects'] if o['kind'] == 'source_tiff']
    assert len(source_objects) == 1
    assert source_objects[0]['size'] == source_size
    run_refs = manifest['runs']
    assert {r['source_hash'] for r in run_refs} == {source_objects[0]['hash']}

    # The sealed total fits one source + two outputs, and the savings line
    # records the size of the second (not written) source copy.
    assert budget['deduplicated_source_bytes'] == source_size
    assert budget['object_bytes'] == source_size + outputs + sum(
        o['size'] for o in manifest['objects']
        if o['kind'] in ('contract_body', 'evidence'))
    assert budget['total_bytes'] <= dedup_budget(scenario)
    # A per-run copy scheme would need two source copies and cannot fit.
    assert budget['total_bytes'] < per_run_budget_floor(scenario)
    assert per_run_budget_floor(scenario) > dedup_budget(scenario)


def test_budget_shortfall_refuses_without_bundle_or_staging(scenario):
    archive.request(scenario['db_path'], 'arc-tight', scenario['run_ids'],
                    'full_replay')
    root = scenario['archive_root']
    # Budget large enough for product-only, smaller than even one full bundle.
    too_small = scenario['output_sizes'][0] + 1024
    with pytest.raises(archive.ArchiveError, match='byte_budget_exceeded'):
        archive.publish('arc-tight', root, too_small, db_path=scenario['db_path'])
    # No formal directory and no private staging residue may survive.
    assert not (root / 'arc-tight').exists()
    assert not [p for p in root.iterdir() if p.name.startswith(archive.STAGING_PREFIX)]


def test_budget_that_only_fits_dedup_package_seals(scenario):
    # Budget between the dedup total and the naive two-source total.
    manifest = _publish(scenario, 'arc-edge', scenario['run_ids'], 'full_replay',
                        budget=dedup_budget(scenario))
    assert manifest['budget']['total_bytes'] <= dedup_budget(scenario)
    assert manifest['budget']['total_bytes'] + scenario['source_size'] \
        > dedup_budget(scenario)


# ---------------------------------------------------------------------------
# full_replay: independent restore and fresh-ledger audit
# ---------------------------------------------------------------------------
def test_full_replay_restores_and_audits_after_online_files_deleted(scenario):
    manifest = _publish(scenario, 'arc-replay', scenario['run_ids'], 'full_replay')
    bundle = scenario['archive_root'] / 'arc-replay'
    report = archive.verify_bundle(bundle)
    assert report['guarantee'] == archive.GUARANTEE_REPLAY
    assert 'fresh_ledger_audit' in report['verified']

    # Remove every original online raster: the bundle must be self-sufficient.
    scenario['source'].unlink()
    for out in scenario['outputs']:
        out.unlink()

    target = scenario['db_path'].parent / 'restore'
    restored = archive.restore(bundle, target / 'fresh.db', target / 'files')
    assert restored['guarantee'] == archive.GUARANTEE_REPLAY
    assert restored['display_only'] is False

    fresh = connect(restored['db_path'])
    try:
        for archived_rid in scenario['run_ids']:
            new_rid = restored['run_id_map'][archived_rid]
            row = fresh.execute('SELECT status FROM run WHERE id=?', (new_rid,)).fetchone()
            assert row['status'] == '已核验'
            # Full chain rebuilt on fresh integer ids and fresh file paths.
            chain = fresh.execute('''SELECT s.path AS sp,s.hash AS sh,o.path AS op,
                o.hash AS oh,c.body AS body FROM run r
                JOIN source s ON s.id=r.source_id
                JOIN contract c ON c.id=r.contract_id
                JOIN output o ON o.run_id=r.id WHERE r.id=?''', (new_rid,)).fetchone()
            assert Path(chain['sp']).is_file() and Path(chain['op']).is_file()
            assert str(target) in chain['sp'] and str(target) in chain['op']
            n_cells = fresh.execute('SELECT count(*) FROM cell WHERE run_id=?',
                                    (new_rid,)).fetchone()[0]
            assert n_cells == 32 * 32
    finally:
        fresh.close()

    # Independent audit on the brand new ledger stays clean for both runs.
    for archived_rid in scenario['run_ids']:
        assert engine.audit(restored['db_path'],
                            restored['run_id_map'][archived_rid]) == []
    # Both runs share one restored source byte store (deduplication survives).
    shared = list(Path(restored['files_dir']).glob('source-*.tif'))
    assert len(shared) == 1
    assert manifest['seal']


def test_restore_never_mutates_original_ledger_or_outputs(scenario):
    _publish(scenario, 'arc-replay', scenario['run_ids'], 'full_replay')
    original_bytes = scenario['source'].read_bytes()
    original_output = scenario['outputs'][0].read_bytes()
    db = connect(scenario['db_path'])
    before = [dict(r) for r in db.execute('SELECT id,path,hash FROM source')]
    before_out = [dict(r) for r in db.execute('SELECT run_id,path,hash FROM output')]
    db.close()

    target = scenario['db_path'].parent / 'restore'
    archive.restore(scenario['archive_root'] / 'arc-replay',
                    target / 'fresh.db', target / 'files')

    db = connect(scenario['db_path'])
    after = [dict(r) for r in db.execute('SELECT id,path,hash FROM source')]
    after_out = [dict(r) for r in db.execute('SELECT run_id,path,hash FROM output')]
    db.close()
    assert before == after and before_out == after_out
    assert scenario['source'].read_bytes() == original_bytes
    assert scenario['outputs'][0].read_bytes() == original_output


def test_restore_rejects_existing_targets(scenario):
    _publish(scenario, 'arc-replay', scenario['run_ids'], 'full_replay')
    target = scenario['db_path'].parent / 'restore'
    (target / 'files').mkdir(parents=True)
    blocker = target / 'files' / 'blocker'
    blocker.write_bytes(b'x')
    with pytest.raises(archive.ArchiveError, match='restore_target_exists'):
        archive.restore(scenario['archive_root'] / 'arc-replay',
                        target / 'fresh.db', target / 'files')
    assert blocker.read_bytes() == b'x'


# ---------------------------------------------------------------------------
# product_only: honest display-only guarantee
# ---------------------------------------------------------------------------
def test_product_only_restores_display_output_without_source(scenario):
    rid = scenario['run_ids'][0]
    manifest = _publish(scenario, 'arc-prod', [rid], 'product_only')
    bundle = scenario['archive_root'] / 'arc-prod'
    report = archive.verify_bundle(bundle)

    assert manifest['guarantee'] == archive.GUARANTEE_DISPLAY
    assert not any(o['kind'] == 'source_tiff' for o in manifest['objects'])
    assert report['guarantee'] == archive.GUARANTEE_DISPLAY
    assert 'source_reverification' in report['not_claimed']
    assert report['verified'] == ['output_openable', 'output_grid_and_hash',
                                  'manifest_seal', 'object_hashes']

    # Delete the original source; display restore must still work.
    scenario['source'].unlink()
    target = scenario['db_path'].parent / 'p-restore'
    restored = archive.restore(bundle, target / 'fresh.db', target / 'files')
    assert restored['display_only'] is True
    out = Path(restored['output_path_map'][rid])
    assert out.is_file()
    with rasterio.open(out) as tif:
        assert tif.width == 32 and tif.height == 32
    assert engine.sha(out.read_bytes()) == manifest['runs'][0]['output_hash']


def test_product_only_bundle_never_reports_replay_audit(scenario):
    rid = scenario['run_ids'][0]
    manifest = _publish(scenario, 'arc-prod', [rid], 'product_only')
    raw = json.loads((scenario['archive_root'] / 'arc-prod' / 'manifest.json').read_text())
    assert raw['guarantee'] != archive.GUARANTEE_REPLAY
    report = archive.verify_bundle(scenario['archive_root'] / 'arc-prod')
    assert 'fresh_ledger_audit' not in report['verified']
    assert manifest['policy'] == 'product_only'


# ---------------------------------------------------------------------------
# Tampering: seal, objects, evidence and restored output
# ---------------------------------------------------------------------------
def test_manifest_tampering_breaks_seal(scenario):
    _publish(scenario, 'arc-replay', scenario['run_ids'], 'full_replay')
    bundle = scenario['archive_root'] / 'arc-replay'
    manifest_path = bundle / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['budget']['byte_budget'] += 1
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    with pytest.raises(archive.ArchiveError, match='seal_broken'):
        archive.verify_bundle(bundle)
    with pytest.raises(archive.ArchiveError, match='seal_broken'):
        archive.restore(bundle, scenario['db_path'].parent / 'x' / 'd.db',
                        scenario['db_path'].parent / 'x' / 'f')


def test_shared_source_object_corruption_is_rejected(scenario):
    manifest = _publish(scenario, 'arc-replay', scenario['run_ids'], 'full_replay')
    source_hash = next(o['hash'] for o in manifest['objects']
                       if o['kind'] == 'source_tiff')
    obj = scenario['archive_root'] / 'arc-replay' / 'objects' / source_hash
    obj.write_bytes(obj.read_bytes() + b'corrupt')
    with pytest.raises(archive.ArchiveError, match='object_corrupt'):
        archive.verify_bundle(scenario['archive_root'] / 'arc-replay')
    with pytest.raises(archive.ArchiveError, match='object_corrupt'):
        archive.restore(scenario['archive_root'] / 'arc-replay',
                        scenario['db_path'].parent / 'x' / 'd.db',
                        scenario['db_path'].parent / 'x' / 'f')


def test_deleted_shared_object_is_rejected(scenario):
    manifest = _publish(scenario, 'arc-replay', scenario['run_ids'], 'full_replay')
    source_hash = next(o['hash'] for o in manifest['objects']
                       if o['kind'] == 'source_tiff')
    (scenario['archive_root'] / 'arc-replay' / 'objects' / source_hash).unlink()
    with pytest.raises(archive.ArchiveError, match='object_missing'):
        archive.verify_bundle(scenario['archive_root'] / 'arc-replay')


def test_output_tiff_tampering_rejected_even_in_product_only(scenario):
    rid = scenario['run_ids'][0]
    manifest = _publish(scenario, 'arc-prod', [rid], 'product_only')
    out_hash = manifest['runs'][0]['output_hash']
    obj = scenario['archive_root'] / 'arc-prod' / 'objects' / out_hash
    obj.write_bytes(b'not a tiff anymore')
    with pytest.raises(archive.ArchiveError, match='object_corrupt'):
        archive.verify_bundle(scenario['archive_root'] / 'arc-prod')


def test_cell_evidence_tampering_rejected(scenario):
    manifest = _publish(scenario, 'arc-replay', scenario['run_ids'], 'full_replay')
    ev_hash = manifest['runs'][0]['evidence_hash']
    obj = scenario['archive_root'] / 'arc-replay' / 'objects' / ev_hash
    evidence = json.loads(gzip.decompress(obj.read_bytes()))
    evidence['cells'][0]['value'] = 999999.0
    # Keep the object's on-disk hash consistent with a new content-addressed
    # name only in the manifest would require a valid seal; here mutating the
    # bytes alone must already fail hash verification.
    obj.write_bytes(gzip.compress(json.dumps(
        evidence, sort_keys=True, separators=(',', ':')).encode()))
    with pytest.raises(archive.ArchiveError, match='object_corrupt'):
        archive.verify_bundle(scenario['archive_root'] / 'arc-replay')


def test_restored_output_tampering_fails_fresh_audit(scenario):
    _publish(scenario, 'arc-replay', scenario['run_ids'], 'full_replay')
    scenario['source'].unlink()
    for out in scenario['outputs']:
        out.unlink()
    target = scenario['db_path'].parent / 'restore'
    restored = archive.restore(scenario['archive_root'] / 'arc-replay',
                               target / 'fresh.db', target / 'files')
    out = Path(restored['output_path_map'][scenario['run_ids'][0]])
    out.write_bytes(b'tampered after restore')
    assert 'output_hash' in engine.audit(
        restored['db_path'], restored['run_id_map'][scenario['run_ids'][0]])


# ---------------------------------------------------------------------------
# Copy-time source replacement (TOCTOU)
# ---------------------------------------------------------------------------
def test_source_replaced_between_audit_and_copy_is_rejected(scenario, monkeypatch):
    archive.request(scenario['db_path'], 'arc-race', scenario['run_ids'],
                    'full_replay')
    real_source = scenario['source']
    original = real_source.read_bytes()

    real_read = archive._read_object_bytes

    def swap_then_read(path):
        data = real_read(path)
        if Path(path) == real_source and not getattr(swap_then_read, 'done', False):
            swap_then_read.done = True
            # Simulate the source path being swapped after the frozen audit
            # but while the archive is copying its bytes.
            real_source.write_bytes(b'replaced-after-audit')
            return real_read(path)
        return data

    monkeypatch.setattr(archive, '_read_object_bytes', swap_then_read)
    with pytest.raises(archive.ArchiveError, match='object_changed_after_audit'):
        archive.publish('arc-race', scenario['archive_root'],
                        dedup_budget(scenario), db_path=scenario['db_path'])
    # Restore the original so teardown stays clean; no bundle was published.
    real_source.write_bytes(original)
    assert not (scenario['archive_root'] / 'arc-race').exists()


# ---------------------------------------------------------------------------
# Missing cell evidence and non-verified runs
# ---------------------------------------------------------------------------
def test_run_with_missing_cells_cannot_be_sealed(scenario):
    rid = scenario['run_ids'][1]
    with connect(scenario['db_path']) as db:
        db.execute('DELETE FROM cell WHERE run_id=? AND row=0', (rid,))
    with pytest.raises(archive.ArchiveError, match='cell_evidence_missing|run_audit_failed'):
        _publish(scenario, 'arc-broken', [rid], 'full_replay')
    assert not (scenario['archive_root'] / 'arc-broken').exists()


def test_unverified_run_refused(scenario):
    rid = scenario['run_ids'][0]
    scenario['outputs'][0].write_bytes(b'corrupt output')
    with pytest.raises(archive.ArchiveError, match='run_audit_failed'):
        _publish(scenario, 'arc-bad', [rid], 'full_replay')


# ---------------------------------------------------------------------------
# Atomic publication, idempotency and crash residue
# ---------------------------------------------------------------------------
def test_repeat_publish_is_idempotent_and_never_overwrites(scenario):
    manifest = _publish(scenario, 'arc-replay', scenario['run_ids'], 'full_replay')
    bundle = scenario['archive_root'] / 'arc-replay'
    seal_file = bundle / 'manifest.json'
    mtime = seal_file.stat().st_mtime_ns

    again = archive.publish('arc-replay', scenario['archive_root'],
                            dedup_budget(scenario), db_path=scenario['db_path'])
    assert again['seal'] == manifest['seal']
    assert seal_file.stat().st_mtime_ns == mtime
    assert archive.verify_bundle(bundle)['guarantee'] == archive.GUARANTEE_REPLAY


def test_crash_residue_is_cleaned_and_retry_succeeds(scenario):
    archive.request(scenario['db_path'], 'arc-crash', scenario['run_ids'],
                    'full_replay')
    root = scenario['archive_root']
    stale = root / (archive.STAGING_PREFIX + 'arc-crash')
    stale.mkdir()
    (stale / 'leftover').write_bytes(b'partial')
    manifest = archive.publish('arc-crash', root, dedup_budget(scenario),
                               db_path=scenario['db_path'])
    assert not stale.exists()
    assert (root / 'arc-crash' / 'manifest.json').is_file()
    assert manifest['seal']


def test_failed_publish_removes_private_staging(scenario, monkeypatch):
    archive.request(scenario['db_path'], 'arc-fail', scenario['run_ids'],
                    'full_replay')

    def boom(*args, **kwargs):
        raise OSError('disk vanished during sealing')

    monkeypatch.setattr(archive.os, 'replace', boom)
    with pytest.raises(OSError, match='disk vanished'):
        archive.publish('arc-fail', scenario['archive_root'],
                        dedup_budget(scenario), db_path=scenario['db_path'])
    root = scenario['archive_root']
    assert not (root / 'arc-fail').exists()
    assert not [p for p in root.iterdir() if p.name.startswith(archive.STAGING_PREFIX)]


def test_corrupt_existing_bundle_is_not_overwritten(scenario):
    manifest = _publish(scenario, 'arc-replay', scenario['run_ids'], 'full_replay')
    bundle = scenario['archive_root'] / 'arc-replay'
    (bundle / 'objects' / manifest['objects'][0]['hash']).unlink()
    with pytest.raises(archive.ArchiveError, match='object_missing'):
        archive.publish('arc-replay', scenario['archive_root'],
                        dedup_budget(scenario), db_path=scenario['db_path'])
    # The corrupt directory is left in place rather than silently replaced.
    assert bundle.is_dir()


# ---------------------------------------------------------------------------
# Frozen consistent view: old cells cannot be stitched to a new output
# ---------------------------------------------------------------------------
def test_snapshot_mixes_no_old_cells_with_new_output(scenario):
    # The frozen view must read cell and output rows from one transaction.
    # A second writer replacing the output row during sealing would either be
    # blocked by the IMMEDIATE lock or fail the in-snapshot hash comparison;
    # assert the helper itself presents one coherent snapshot.
    rid = scenario['run_ids'][0]
    with connect(scenario['db_path']) as db:
        snap, cells, issues = archive._snapshot_run(db, rid)
    assert len(cells) == 32 * 32
    assert snap['output_hash'] == engine.sha(
        Path(snap['output_path']).read_bytes())
    assert all(set(c) == {'row', 'col', 'value', 'coverage',
                          'source_row', 'source_col', 'weight'} for c in cells)
    assert isinstance(issues, list)


def test_frozen_view_blocks_concurrent_ledger_writes(scenario):
    """While a seal holds the IMMEDIATE snapshot, a second connection cannot
    commit new cell/output rows, so old evidence can never be stitched to a
    replaced output file."""
    import sqlite3
    import threading
    import time

    from gridreproject.store import frozen_view

    rid = scenario['run_ids'][0]
    entered = threading.Event()
    release = threading.Event()
    writer_error = []

    def concurrent_writer():
        entered.wait()
        other = sqlite3.connect(str(scenario['db_path']), timeout=1.0)
        try:
            other.execute('UPDATE cell SET value=value+1 WHERE run_id=?', (rid,))
            other.commit()
        except sqlite3.OperationalError as exc:
            writer_error.append(str(exc))
        finally:
            other.close()
            release.set()

    thread = threading.Thread(target=concurrent_writer)
    thread.start()
    with frozen_view(scenario['db_path']) as db:
        entered.set()
        # Read inside the frozen snapshot while the writer is blocked.
        before = db.execute(
            'SELECT sum(value) FROM cell WHERE run_id=?', (rid,)).fetchone()[0]
        time.sleep(1.2)
        inside = db.execute(
            'SELECT sum(value) FROM cell WHERE run_id=?', (rid,)).fetchone()[0]
    thread.join()
    release.wait(timeout=2)
    assert before == inside
    assert writer_error and 'locked' in writer_error[0]


# ---------------------------------------------------------------------------
# Legacy request/export, runs and release records stay unchanged
# ---------------------------------------------------------------------------
def test_legacy_export_and_ledger_records_unchanged(scenario):
    rid = scenario['run_ids'][0]
    archive.request(scenario['db_path'], 'legacy-arc', [rid], 'full_replay')
    legacy_dir = scenario['db_path'].parent / 'legacy-bundle'
    result = archive.export(scenario['db_path'], 'legacy-arc', legacy_dir)
    assert result['runs'][0]['source_file'] == f'run-{rid}-source.tif'
    assert (legacy_dir / 'manifest.json').is_file()
    with pytest.raises(ValueError, match='archive_exists'):
        archive.export(scenario['db_path'], 'legacy-arc', legacy_dir)

    db = connect(scenario['db_path'])
    try:
        # Sealing the new-style bundle adds no rows to business tables.
        counts_before = {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                         for t in ('source', 'contract', 'run', 'cell',
                                   'issue', 'output')}
    finally:
        db.close()
    _publish(scenario, 'arc-replay', scenario['run_ids'], 'full_replay')
    db = connect(scenario['db_path'])
    try:
        counts_after = {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                        for t in ('source', 'contract', 'run', 'cell',
                                  'issue', 'output')}
    finally:
        db.close()
    assert counts_before == counts_after


def test_legacy_request_conflict_still_enforced(scenario):
    rid = scenario['run_ids'][0]
    archive.request(scenario['db_path'], 'arc-1', [rid], 'full_replay')
    with pytest.raises(ValueError, match='archive_request_conflict'):
        archive.request(scenario['db_path'], 'arc-1', [rid], 'product_only')


# ---------------------------------------------------------------------------
# Malformed bundles and strengthened structural checks
# ---------------------------------------------------------------------------
def test_unknown_format_and_missing_manifest_rejected(scenario):
    _publish(scenario, 'arc-replay', scenario['run_ids'], 'full_replay')
    bundle = scenario['archive_root'] / 'arc-replay'
    (bundle / 'manifest.json').unlink()
    with pytest.raises(archive.ArchiveError, match='manifest_missing'):
        archive.verify_bundle(bundle)

    other = scenario['archive_root'] / 'empty-bundle'
    other.mkdir()
    with pytest.raises(archive.ArchiveError, match='manifest_missing'):
        archive.verify_bundle(other)


def test_cell_count_mismatch_with_contract_rejected(scenario):
    manifest = _publish(scenario, 'arc-replay', scenario['run_ids'], 'full_replay')
    ev_hash = manifest['runs'][0]['evidence_hash']
    obj = scenario['archive_root'] / 'arc-replay' / 'objects' / ev_hash
    evidence = json.loads(gzip.decompress(obj.read_bytes()))
    evidence['cells'].pop()
    rewritten = gzip.compress(json.dumps(
        evidence, sort_keys=True, separators=(',', ':')).encode())
    # Re-address the object under its new hash and point the manifest at it;
    # the seal must then fail, and with a re-sealed manifest the structural
    # cell-count cross-check must still reject the forged bundle.
    new_hash = engine.sha(rewritten)
    (scenario['archive_root'] / 'arc-replay' / 'objects' / new_hash).write_bytes(rewritten)
    manifest_path = scenario['archive_root'] / 'arc-replay' / 'manifest.json'
    manifest['runs'][0]['evidence_hash'] = new_hash
    for record in manifest['objects']:
        if record['hash'] == ev_hash:
            record['hash'] = new_hash
            record['size'] = len(rewritten)
    manifest['seal'] = archive._seal_manifest(manifest)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    with pytest.raises(archive.ArchiveError,
                       match='object_missing|cell_evidence_mismatch|dangling_reference'):
        archive.verify_bundle(scenario['archive_root'] / 'arc-replay')


def test_output_bounds_tampering_rejected(scenario):
    rid = scenario['run_ids'][0]
    manifest = _publish(scenario, 'arc-prod', [rid], 'product_only')
    # Forge a valid-seal bundle whose output keeps the same hash name but the
    # contract claims different bounds: grid conformity must not be trusted
    # from the manifest alone.
    bundle = scenario['archive_root'] / 'arc-prod'
    c_hash = manifest['runs'][0]['contract_hash']
    contract = json.loads((bundle / 'objects' / c_hash).read_bytes())
    contract['bounds'] = [0, 0, 99, 99]
    body = json.dumps(contract, sort_keys=True, separators=(',', ':')).encode()
    new_c_hash = engine.sha(body)
    (bundle / 'objects' / new_c_hash).write_bytes(body)
    manifest['runs'][0]['contract_hash'] = new_c_hash
    manifest['objects'].append({'hash': new_c_hash, 'kind': 'contract_body',
                                'size': len(body), 'encoding': 'identity'})
    manifest['seal'] = archive._seal_manifest(manifest)
    (bundle / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True))
    with pytest.raises(archive.ArchiveError,
                       match='bounds_mismatch|evidence_contract_mismatch'):
        archive.verify_bundle(bundle)


# ---------------------------------------------------------------------------
# CLI smoke test (isolated mode, offline)
# ---------------------------------------------------------------------------
def test_cli_publish_verify_restore(scenario):
    rid = scenario['run_ids'][0]
    archive.request(scenario['db_path'], 'cli-arc', [rid], 'full_replay')
    env = dict(os.environ)
    base = [sys.executable, '-I', '-m', 'gridreproject']

    pub = subprocess.run(base + [
        'archive-publish', '--db', str(scenario['db_path']), '--id', 'cli-arc',
        '--archive-root', str(scenario['archive_root']),
        '--byte-budget', str(dedup_budget(scenario))],
        capture_output=True, text=True, env=env, check=True)
    assert '"seal"' in pub.stdout
    bundle = scenario['archive_root'] / 'cli-arc'

    ver = subprocess.run(base + ['archive-verify', '--bundle', str(bundle)],
                         capture_output=True, text=True, env=env, check=True)
    assert archive.GUARANTEE_REPLAY in ver.stdout

    target = scenario['db_path'].parent / 'cli-restore'
    res = subprocess.run(base + [
        'archive-restore', '--bundle', str(bundle),
        '--fresh-db', str(target / 'fresh.db'),
        '--fresh-files', str(target / 'files')],
        capture_output=True, text=True, env=env, check=True)
    assert '"display_only": false' in res.stdout
    assert (target / 'fresh.db').is_file()
