import json
import sqlite3

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from gridreproject import engine, mosaic
from gridreproject.store import connect

ND = -9999.0


def write_tif(path, data, bounds, nodata=-9999):
    data = np.array(data, dtype='float64')
    h, w = data.shape
    with rasterio.open(path, 'w', driver='GTiff', width=w, height=h, count=1,
                       dtype='float64', crs='EPSG:4326',
                       transform=from_bounds(*bounds, w, h), nodata=nodata) as dst:
        dst.write(data, 1)
    return path


def counterexample(tmp_path, a_row=None, b_row=None):
    """契约反例构造：A 有效覆盖 0.75 取值 10，B 有效覆盖 0.375 取值 30。

    权重比 0.75:0.375 = 0.8:0.4，与契约反例同解：priority 得 10，
    quality_weighted 得 (10*0.75+30*0.375)/1.125 = 16.666667。
    """
    a = write_tif(tmp_path/'a.tif', [a_row if a_row is not None else [ND, 10, 10, ND]]*4,
                  [-1, -1, 3, 3])
    b = write_tif(tmp_path/'b.tif', [b_row if b_row is not None else [ND, ND, 30, ND]]*4,
                  [-1, -1, 3, 3])
    return a, b


def mcfg(tmp_path, policy='priority', min_coverage=0.3, **kw):
    c = {'crs': 'EPSG:4326', 'bounds': [0, 0, 2, 2], 'width': 1, 'height': 1,
         'kind': 'continuous', 'method': 'bilinear', 'min_coverage': min_coverage,
         'overlap_policy': policy,
         'sources': [{'id': 'A', 'path': str(tmp_path/'a.tif')},
                     {'id': 'B', 'path': str(tmp_path/'b.tif')}]}
    c.update(kw)
    return c


def seam(tmp_path, b_valid=5, b_val=20):
    """左右接边：A 覆盖左半取值 10，B 覆盖右半取值 b_val，缝合格各半覆盖。"""
    row_a = [10]*5 + [ND]*7
    row_b = [ND]*5 + [b_val]*b_valid + [ND]*(7-b_valid)
    write_tif(tmp_path/'sa.tif', [row_a]*6, [-1, -1, 5, 2])
    write_tif(tmp_path/'sb.tif', [row_b]*6, [-1, -1, 5, 2])


def seam_cfg(tmp_path, width=3, policy='priority', min_coverage=0.3, **kw):
    return mcfg(tmp_path, policy, min_coverage, bounds=[0, 0, width, 1], width=width,
                sources=[{'id': 'A', 'path': str(tmp_path/'sa.tif')},
                         {'id': 'B', 'path': str(tmp_path/'sb.tif')}], **kw)


def cells(db, rid):
    dbh = connect(db)
    rows = [dict(x) for x in dbh.execute(
        'SELECT * FROM mosaic_cell WHERE run_id=? ORDER BY row,col', (rid,))]
    dbh.close()
    return rows


def contribs(db, rid, row, col):
    dbh = connect(db)
    rows = [dict(x) for x in dbh.execute(
        'SELECT * FROM mosaic_contrib WHERE run_id=? AND row=? AND col=? ORDER BY position',
        (rid, row, col))]
    dbh.close()
    return rows


def status(db, rid):
    dbh = connect(db)
    s = dbh.execute('SELECT status FROM mosaic_run WHERE id=?', (rid,)).fetchone()[0]
    dbh.close()
    return s


def test_priority_picks_first_qualified_source(tmp_path):
    counterexample(tmp_path)
    rid = mosaic.run(tmp_path/'db', mcfg(tmp_path, 'priority'), tmp_path/'out.tif')
    assert mosaic.audit(tmp_path/'db', rid) == []
    cell, = cells(tmp_path/'db', rid)
    assert cell['decision'] == 'overlap'
    assert cell['value'] == pytest.approx(10.0)
    a, b = contribs(tmp_path/'db', rid, 0, 0)
    assert a['stable_id'] == 'A' and a['candidate'] == 1
    assert a['coverage'] == pytest.approx(0.75)
    assert a['weight'] == 1.0
    assert b['stable_id'] == 'B' and b['candidate'] == 1
    assert b['coverage'] == pytest.approx(0.375)
    assert b['weight'] == 0.0
    with rasterio.open(tmp_path/'out.tif') as ds:
        assert ds.read(1)[0, 0] == pytest.approx(10.0)


def test_quality_weighted_counterexample(tmp_path):
    counterexample(tmp_path)
    rid = mosaic.run(tmp_path/'db', mcfg(tmp_path, 'quality_weighted'), tmp_path/'out.tif')
    cell, = cells(tmp_path/'db', rid)
    assert cell['decision'] == 'overlap'
    assert cell['value'] == pytest.approx(16.666667, abs=1e-6)
    a, b = contribs(tmp_path/'db', rid, 0, 0)
    assert a['weight'] == pytest.approx(2/3)
    assert b['weight'] == pytest.approx(1/3)
    assert a['value'] == pytest.approx(10.0)
    assert b['value'] == pytest.approx(30.0)
    assert mosaic.audit(tmp_path/'db', rid) == []


def test_policies_give_different_results_on_same_sources(tmp_path):
    counterexample(tmp_path)
    rp = mosaic.run(tmp_path/'db', mcfg(tmp_path, 'priority'), tmp_path/'p.tif')
    rw = mosaic.run(tmp_path/'db', mcfg(tmp_path, 'quality_weighted'), tmp_path/'w.tif')
    vp, = cells(tmp_path/'db', rp)
    vw, = cells(tmp_path/'db', rw)
    assert vp['value'] == pytest.approx(10.0)
    assert vw['value'] == pytest.approx(16.666667, abs=1e-6)


def test_priority_falls_back_when_first_source_nodata(tmp_path):
    counterexample(tmp_path, a_row=[ND, ND, ND, ND])
    rid = mosaic.run(tmp_path/'db', mcfg(tmp_path, 'priority'), tmp_path/'out.tif')
    cell, = cells(tmp_path/'db', rid)
    assert cell['decision'] == 'single'
    assert cell['value'] == pytest.approx(30.0)
    a, b = contribs(tmp_path/'db', rid, 0, 0)
    assert a['candidate'] == 0 and a['weight'] is None
    assert b['candidate'] == 1 and b['weight'] == 1.0


def test_priority_skips_source_below_coverage_threshold(tmp_path):
    # A 覆盖 0.375 低于门槛 0.5，B 覆盖 0.75 达标：不得只按源列表首项写值。
    counterexample(tmp_path, a_row=[ND, ND, 10, ND], b_row=[ND, 30, 30, ND])
    rid = mosaic.run(tmp_path/'db', mcfg(tmp_path, 'priority', min_coverage=0.5),
                     tmp_path/'out.tif')
    cell, = cells(tmp_path/'db', rid)
    assert cell['decision'] == 'single'
    assert cell['value'] == pytest.approx(30.0)
    a, b = contribs(tmp_path/'db', rid, 0, 0)
    assert a['candidate'] == 0
    assert b['weight'] == 1.0


def test_seam_half_coverage_overlap(tmp_path):
    seam(tmp_path)
    rid = mosaic.run(tmp_path/'db', seam_cfg(tmp_path, policy='quality_weighted'),
                     tmp_path/'out.tif')
    got = cells(tmp_path/'db', rid)
    assert [c['decision'] for c in got] == ['single', 'overlap', 'single']
    assert got[0]['value'] == pytest.approx(10.0)
    assert got[1]['value'] == pytest.approx(15.0)
    assert got[2]['value'] == pytest.approx(20.0)
    a, b = contribs(tmp_path/'db', rid, 0, 1)
    assert a['coverage'] == pytest.approx(0.5)
    assert b['coverage'] == pytest.approx(0.5)
    assert a['weight'] == pytest.approx(0.5)
    assert b['weight'] == pytest.approx(0.5)
    assert mosaic.audit(tmp_path/'db', rid) == []


def test_seam_below_threshold_marks_gap(tmp_path):
    seam(tmp_path)
    rid = mosaic.run(tmp_path/'db', seam_cfg(tmp_path, min_coverage=0.6), tmp_path/'out.tif')
    got = cells(tmp_path/'db', rid)
    assert [c['decision'] for c in got] == ['single', 'gap', 'single']
    assert got[1]['value'] is None
    with rasterio.open(tmp_path/'out.tif') as ds:
        assert ds.read(1)[0, 1] == ds.nodata
    dbh = connect(tmp_path/'db')
    assert dbh.execute("SELECT count(*) FROM mosaic_issue WHERE run_id=? AND code='no_source'",
                       (rid,)).fetchone()[0] == 1
    dbh.close()
    assert status(tmp_path/'db', rid) == '待核'


def test_category_rejects_quality_weighted(tmp_path):
    counterexample(tmp_path)
    with pytest.raises(ValueError, match='policy_kind_conflict'):
        mosaic.freeze(tmp_path/'db', mcfg(tmp_path, 'quality_weighted',
                                          kind='category', method='nearest'))


def test_extensive_rejects_quality_weighted(tmp_path):
    counterexample(tmp_path)
    with pytest.raises(ValueError, match='policy_kind_conflict'):
        mosaic.freeze(tmp_path/'db', mcfg(tmp_path, 'quality_weighted',
                                          kind='extensive', method='area'))


def test_bad_overlap_policy_rejected(tmp_path):
    counterexample(tmp_path)
    with pytest.raises(ValueError, match='bad_overlap_policy'):
        mosaic.freeze(tmp_path/'db', mcfg(tmp_path, 'average'))


def test_extensive_priority_preserves_total_semantics(tmp_path):
    write_tif(tmp_path/'a.tif', [[10, 10], [10, 10]], [0, 0, 2, 2])
    write_tif(tmp_path/'b.tif', [[30, 30], [30, 30]], [0, 0, 2, 2])
    rid = mosaic.run(tmp_path/'db', mcfg(tmp_path, 'priority', kind='extensive', method='area'),
                     tmp_path/'out.tif')
    cell, = cells(tmp_path/'db', rid)
    assert cell['decision'] == 'overlap'
    assert cell['value'] == pytest.approx(40.0)  # A 的总量，而非两源平均 80
    assert mosaic.audit(tmp_path/'db', rid) == []


def test_category_priority_run(tmp_path):
    write_tif(tmp_path/'a.tif', [[7]], [0, 0, 2, 2])
    write_tif(tmp_path/'b.tif', [[9]], [0, 0, 2, 2])
    rid = mosaic.run(tmp_path/'db', mcfg(tmp_path, 'priority', kind='category',
                                         method='nearest'), tmp_path/'out.tif')
    cell, = cells(tmp_path/'db', rid)
    assert cell['decision'] == 'overlap'
    assert cell['value'] == pytest.approx(7.0)
    assert mosaic.audit(tmp_path/'db', rid) == []


def test_identity_tracks_policy_order_threshold_and_content(tmp_path):
    counterexample(tmp_path)
    base = mosaic.freeze(tmp_path/'db', mcfg(tmp_path))
    assert mosaic.freeze(tmp_path/'db', mcfg(tmp_path)) == base
    assert mosaic.freeze(tmp_path/'db', mcfg(tmp_path, 'quality_weighted')) != base
    swapped = mcfg(tmp_path)
    swapped['sources'] = [swapped['sources'][1], swapped['sources'][0]]
    assert mosaic.freeze(tmp_path/'db', swapped) != base
    assert mosaic.freeze(tmp_path/'db', mcfg(tmp_path, min_coverage=0.4)) != base
    write_tif(tmp_path/'a.tif', [[ND, 11, 11, ND]]*4, [-1, -1, 3, 3])  # 同标识换内容
    assert mosaic.freeze(tmp_path/'db', mcfg(tmp_path)) != base


def test_pinned_source_identity_conflict(tmp_path):
    a, _ = counterexample(tmp_path)
    pinned = mcfg(tmp_path)
    pinned['sources'][0]['sha256'] = engine.sha(a.read_bytes())
    assert mosaic.freeze(tmp_path/'db', pinned)
    write_tif(tmp_path/'a.tif', [[ND, 12, 12, ND]]*4, [-1, -1, 3, 3])
    with pytest.raises(ValueError, match='source_identity_conflict'):
        mosaic.freeze(tmp_path/'db', pinned)


def test_run_audit_fails_after_source_content_change(tmp_path):
    counterexample(tmp_path)
    rid = mosaic.run(tmp_path/'db', mcfg(tmp_path), tmp_path/'out.tif')
    assert mosaic.audit(tmp_path/'db', rid) == []
    write_tif(tmp_path/'a.tif', [[ND, 99, 99, ND]]*4, [-1, -1, 3, 3])
    issues = mosaic.audit(tmp_path/'db', rid)
    assert 'source_hash' in issues
    assert status(tmp_path/'db', rid) == '失败'


def test_contrib_evidence_tamper_detected(tmp_path):
    counterexample(tmp_path)
    rid = mosaic.run(tmp_path/'db', mcfg(tmp_path, 'quality_weighted'), tmp_path/'out.tif')
    dbh = connect(tmp_path/'db')
    with dbh:
        dbh.execute('UPDATE mosaic_contrib SET weight=0.5 WHERE run_id=? AND position=0', (rid,))
    dbh.close()
    assert 'contrib_mismatch' in mosaic.audit(tmp_path/'db', rid)
    assert status(tmp_path/'db', rid) == '失败'


def test_cell_value_tamper_detected(tmp_path):
    counterexample(tmp_path)
    rid = mosaic.run(tmp_path/'db', mcfg(tmp_path), tmp_path/'out.tif')
    dbh = connect(tmp_path/'db')
    with dbh:
        dbh.execute('UPDATE mosaic_cell SET value=99 WHERE run_id=?', (rid,))
    dbh.close()
    assert 'cell_mismatch' in mosaic.audit(tmp_path/'db', rid)
    assert status(tmp_path/'db', rid) == '失败'


def test_contrib_evidence_delete_detected(tmp_path):
    counterexample(tmp_path)
    rid = mosaic.run(tmp_path/'db', mcfg(tmp_path), tmp_path/'out.tif')
    dbh = connect(tmp_path/'db')
    with dbh:
        dbh.execute('DELETE FROM mosaic_contrib WHERE run_id=? AND position=1', (rid,))
    dbh.close()
    assert 'contrib_evidence_missing' in mosaic.audit(tmp_path/'db', rid)
    assert status(tmp_path/'db', rid) == '失败'


def test_output_tamper_detected(tmp_path):
    counterexample(tmp_path)
    rid = mosaic.run(tmp_path/'db', mcfg(tmp_path), tmp_path/'out.tif')
    (tmp_path/'out.tif').write_bytes(b'bad')
    assert 'output_hash' in mosaic.audit(tmp_path/'db', rid)
    assert status(tmp_path/'db', rid) == '失败'


def test_output_forgery_with_updated_hash_detected(tmp_path):
    counterexample(tmp_path)
    rid = mosaic.run(tmp_path/'db', mcfg(tmp_path), tmp_path/'out.tif')
    with rasterio.open(tmp_path/'out.tif') as ds:
        profile = ds.profile
        data = ds.read(1)
    data[0, 0] += 5
    with rasterio.open(tmp_path/'out.tif', 'w', **profile) as ds:
        ds.write(data, 1)
    dbh = connect(tmp_path/'db')
    with dbh:
        dbh.execute('UPDATE mosaic_output SET hash=? WHERE run_id=?',
                    (engine.sha((tmp_path/'out.tif').read_bytes()), rid))
    dbh.close()
    assert 'cell_mismatch' in mosaic.audit(tmp_path/'db', rid)
    assert status(tmp_path/'db', rid) == '失败'


def single_source_run(tmp_path):
    src = write_tif(tmp_path/'single.tif', [[1, 2], [3, 4]], [0, 0, 2, 2])
    contract = {'crs': 'EPSG:4326', 'bounds': [0, 0, 2, 2], 'width': 2, 'height': 2,
                'kind': 'continuous', 'method': 'bilinear', 'min_coverage': 1.0}
    return engine.run(tmp_path/'db', src, contract, tmp_path/'single-out.tif')


def test_mosaic_never_overwrites_verified_single_source(tmp_path):
    rid = single_source_run(tmp_path)
    assert engine.audit(tmp_path/'db', rid) == []
    before = (tmp_path/'single-out.tif').read_bytes()
    counterexample(tmp_path)
    with pytest.raises(ValueError, match='output_exists'):
        mosaic.run(tmp_path/'db', mcfg(tmp_path), tmp_path/'single-out.tif')
    assert (tmp_path/'single-out.tif').read_bytes() == before
    assert engine.audit(tmp_path/'db', rid) == []
    dbh = connect(tmp_path/'db')
    assert dbh.execute('SELECT count(*) FROM mosaic_run').fetchone()[0] == 0
    dbh.close()


def test_mosaic_publish_failure_rolls_back(tmp_path, monkeypatch):
    rid = single_source_run(tmp_path)
    counterexample(tmp_path)
    def fail_publish(*args):
        raise OSError('simulated_publish_failure')
    monkeypatch.setattr(mosaic.os, 'replace', fail_publish)
    with pytest.raises(OSError, match='simulated_publish_failure'):
        mosaic.run(tmp_path/'db', mcfg(tmp_path), tmp_path/'mosaic-out.tif')
    dbh = connect(tmp_path/'db')
    for table in ('mosaic_run', 'mosaic_cell', 'mosaic_contrib', 'mosaic_output',
                  'mosaic_issue'):
        assert dbh.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    dbh.close()
    assert not (tmp_path/'mosaic-out.tif').exists()
    assert not (tmp_path/'mosaic-out.tmp.tif').exists()
    assert engine.audit(tmp_path/'db', rid) == []


def test_mosaic_evidence_commit_failure_rolls_back(tmp_path, monkeypatch):
    counterexample(tmp_path)
    real_connect = mosaic.connect

    class FailingConn:
        """证据批量写入时抛错的连接代理，模拟 SQLite 提交失败。"""
        def __init__(self, path):
            self._real = real_connect(path)

        def __getattr__(self, name):
            if name == 'executemany':
                def boom(*args, **kw):
                    raise sqlite3.OperationalError('simulated_commit_failure')
                return boom
            return getattr(self._real, name)

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, *exc):
            return self._real.__exit__(*exc)

    monkeypatch.setattr(mosaic, 'connect', FailingConn)
    with pytest.raises(sqlite3.OperationalError, match='simulated_commit_failure'):
        mosaic.run(tmp_path/'db', mcfg(tmp_path), tmp_path/'mosaic-out.tif')
    dbh = connect(tmp_path/'db')
    for table in ('mosaic_run', 'mosaic_cell', 'mosaic_contrib', 'mosaic_output'):
        assert dbh.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    dbh.close()
    assert not (tmp_path/'mosaic-out.tif').exists()
    assert not (tmp_path/'mosaic-out.tmp.tif').exists()


def test_evidence_distinguishes_equal_value_overlap_from_single(tmp_path):
    seam(tmp_path, b_val=10)  # 两源值一致
    rid = mosaic.run(tmp_path/'db', seam_cfg(tmp_path), tmp_path/'out.tif')
    got = cells(tmp_path/'db', rid)
    assert got[0]['value'] == pytest.approx(10.0)
    assert got[1]['value'] == pytest.approx(10.0)
    assert got[0]['decision'] == 'single'
    assert got[1]['decision'] == 'overlap'
    left = contribs(tmp_path/'db', rid, 0, 0)
    middle = contribs(tmp_path/'db', rid, 0, 1)
    assert [x['stable_id'] for x in left if x['candidate']] == ['A']
    assert [x['stable_id'] for x in middle if x['candidate']] == ['A', 'B']


def test_report_summary_samples_and_coordinates(tmp_path):
    seam(tmp_path, b_valid=2)
    rid = mosaic.run(tmp_path/'db', seam_cfg(tmp_path, width=4, policy='quality_weighted'),
                     tmp_path/'out.tif')
    obj = json.loads(mosaic.report(tmp_path/'db', rid, tmp_path/'r.json')
                     .read_text(encoding='utf-8'))
    assert obj['summary']['single'] == 2
    assert obj['summary']['overlap'] == 1
    assert obj['summary']['gap'] == 1
    assert obj['audit'] == []
    assert obj['run']['status'] == '待核'
    samples = obj['summary']['samples']
    assert [(s['row'], s['col']) for s in samples['gap']] == [(0, 3)]
    assert [(s['row'], s['col']) for s in samples['overlap']] == [(0, 1)]
    assert [(s['row'], s['col']) for s in samples['single']] == [(0, 0), (0, 2)]
    cell1 = obj['cells'][1]
    assert cell1['decision'] == 'overlap'
    assert [c['stable_id'] for c in cell1['contribs']] == ['A', 'B']
    assert cell1['x'] == pytest.approx(1.5) and cell1['y'] == pytest.approx(0.5)
    gap = obj['cells'][3]
    assert gap['decision'] == 'gap' and gap['value'] is None
    assert all(c['candidate'] == 0 for c in gap['contribs'])
    assert (tmp_path/'r.md').exists()


def test_missing_or_duplicate_sources_rejected(tmp_path):
    counterexample(tmp_path)
    with pytest.raises(ValueError, match='missing_sources'):
        mosaic.freeze(tmp_path/'db', mcfg(tmp_path, sources=[]))
    dup = mcfg(tmp_path)
    dup['sources'] = [dup['sources'][0], dup['sources'][0]]
    with pytest.raises(ValueError, match='duplicate_source_id'):
        mosaic.freeze(tmp_path/'db', dup)


def test_unknown_mosaic_run_rejected(tmp_path):
    with pytest.raises(ValueError, match='unknown_run'):
        mosaic.audit(tmp_path/'db', 999)
    with pytest.raises(ValueError, match='unknown_run'):
        mosaic.report(tmp_path/'db', 999, tmp_path/'r.json')


def test_cli_mosaic(tmp_path, monkeypatch, capsys):
    from gridreproject.__main__ import main
    counterexample(tmp_path)
    contract = tmp_path/'contract.json'
    contract.write_text(json.dumps(mcfg(tmp_path)), encoding='utf-8')
    monkeypatch.setattr('sys.argv', ['gridreproject', 'mosaic-plan', '--db', str(tmp_path/'db'),
                                     '--contract', str(contract)])
    main()
    assert len(capsys.readouterr().out.strip()) == 64
    monkeypatch.setattr('sys.argv', ['gridreproject', 'mosaic-run', '--db', str(tmp_path/'db'),
                                     '--contract', str(contract), '--out',
                                     str(tmp_path/'out.tif')])
    main()
    rid = int(capsys.readouterr().out.strip())
    monkeypatch.setattr('sys.argv', ['gridreproject', 'mosaic-audit', '--db', str(tmp_path/'db'),
                                     '--run-id', str(rid)])
    main()
    assert json.loads(capsys.readouterr().out) == []
    monkeypatch.setattr('sys.argv', ['gridreproject', 'mosaic-report', '--db', str(tmp_path/'db'),
                                     '--run-id', str(rid), '--path', str(tmp_path/'r.json')])
    main()
    capsys.readouterr()
    obj = json.loads((tmp_path/'r.json').read_text(encoding='utf-8'))
    assert obj['summary']['overlap'] == 1
