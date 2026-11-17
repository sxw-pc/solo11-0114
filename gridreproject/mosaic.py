"""多源拼接运行：有序源冻结、重叠策略、逐格贡献证据与独立复核。

mosaic 契约在单源契约之上增加有序源列表与 overlap_policy；每个来源以稳定标识
登记，字节哈希与地理元数据随契约一并冻结，契约哈希即运行身份。priority 按契约
顺序取第一个覆盖达标的源；quality_weighted 仅对连续量按有效覆盖比例归一加权，
类别量与广延量拒绝该策略（广延量直接平均会改变总量语义）。候选为空的格写
NoData 并标缺口；每格保留全部来源的覆盖、候选、被选权重与逆映射坐标，audit
独立重读所有来源并重算贡献关系，任何篡改都会使运行失去已核验状态。
"""
import json
import os
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling

from .engine import inspect, map_cell, sha
from .store import connect

NODATA = -9999.0
POLICIES = ('priority', 'quality_weighted')
SEVERE = ('source_hash', 'source_meta', 'output_hash', 'grid_mismatch', 'bounds_mismatch',
          'cell_mismatch', 'cell_evidence_missing', 'contrib_mismatch', 'contrib_evidence_missing')


def _check_grid(contract):
    needed = {'crs', 'bounds', 'width', 'height', 'kind', 'method', 'min_coverage'}
    if not needed.issubset(contract):
        raise ValueError('missing_contract')
    x0, y0, x1, y1 = contract['bounds']
    w = int(contract['width']); h = int(contract['height'])
    if w <= 0 or h <= 0 or x1 <= x0 or y1 <= y0 or not 0 <= contract['min_coverage'] <= 1:
        raise ValueError('bad_grid')
    methods = {'category': {'nearest'}, 'continuous': {'bilinear'}, 'extensive': {'area'}}
    if contract['method'] not in methods.get(contract['kind'], set()):
        raise ValueError('method_kind_conflict')
    try:
        rasterio.crs.CRS.from_string(contract['crs'])
    except Exception as exc:
        raise ValueError('bad_crs') from exc


def _check_policy(contract):
    if contract.get('overlap_policy') not in POLICIES:
        raise ValueError('bad_overlap_policy')
    # 类别量不可加权；广延量直接平均会改变总量语义，只能用 priority 取舍。
    if contract['overlap_policy'] == 'quality_weighted' and contract['kind'] in ('category', 'extensive'):
        raise ValueError('policy_kind_conflict')


def freeze(db_path, contract):
    """校验 mosaic 契约，登记并冻结全部来源的哈希与地理元数据，返回契约哈希。

    源条目可带 sha256 钉住内容；内容变化要么生成新契约身份，要么在钉住时
    报 source_identity_conflict，绝不悄悄重用旧来源。
    """
    _check_grid(contract)
    _check_policy(contract)
    sources = contract.get('sources')
    if not isinstance(sources, list) or not sources:
        raise ValueError('missing_sources')
    ids = [s.get('id') if isinstance(s, dict) else None for s in sources]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('duplicate_source_id')
    frozen = []
    for entry in sources:
        if not isinstance(entry, dict) or not entry.get('path'):
            raise ValueError('missing_sources')
        path = Path(entry['path'])
        info = inspect(db_path, path)
        if entry.get('sha256') is not None and entry['sha256'] != info['hash']:
            raise ValueError('source_identity_conflict')
        frozen.append({'id': entry['id'], 'path': str(path), 'sha256': info['hash'],
                       'width': info['width'], 'height': info['height'],
                       'crs': info['crs'], 'nodata': info['nodata']})
    body = json.dumps({'type': 'mosaic', 'crs': contract['crs'], 'bounds': contract['bounds'],
                       'width': int(contract['width']), 'height': int(contract['height']),
                       'kind': contract['kind'], 'method': contract['method'],
                       'min_coverage': contract['min_coverage'],
                       'overlap_policy': contract['overlap_policy'], 'sources': frozen},
                      sort_keys=True, separators=(',', ':'))
    mh = sha(body.encode())
    db = connect(db_path)
    with db:
        db.execute('INSERT OR IGNORE INTO mosaic(hash,body) VALUES (?,?)', (mh, body))
        mid = db.execute('SELECT id FROM mosaic WHERE hash=?', (mh,)).fetchone()['id']
        for pos, src in enumerate(frozen):
            db.execute('INSERT OR IGNORE INTO mosaic_source VALUES (?,?,?,?,?,?,?,?,?)',
                       (mid, pos, src['id'], src['path'], src['sha256'], src['width'],
                        src['height'], src['crs'], src['nodata']))
    db.close()
    return mh


def _layers(body):
    """把每个冻结来源按现有变量语义与 NoData 阈值投到同一目标网格。

    返回每源的 (num, den, 逆映射)；NoData 权重为零不计入数值，源外 den 为负。
    """
    shape = (body['height'], body['width'])
    transform_out = from_bounds(*body['bounds'], body['width'], body['height'])
    method = body['method']
    resampling = {'nearest': Resampling.nearest, 'bilinear': Resampling.bilinear,
                  'area': Resampling.sum}[method]
    layers = []
    for src in body['sources']:
        with rasterio.open(src['path']) as ds:
            source = ds.read(1).astype('float64')
            valid = np.isfinite(source)
            if ds.nodata is not None:
                valid &= source != ds.nodata
            values = np.where(valid, source, 0.0)
            weight = valid.astype('float64')
            num = np.zeros(shape, dtype='float64')
            den = np.zeros(shape, dtype='float64')
            reproject(values, num, src_transform=ds.transform, src_crs=ds.crs,
                      dst_transform=transform_out, dst_crs=body['crs'],
                      resampling=resampling, src_nodata=None, dst_nodata=NODATA)
            cov_resampling = Resampling.average if method == 'area' else resampling
            reproject(weight, den, src_transform=ds.transform, src_crs=ds.crs,
                      dst_transform=transform_out, dst_crs=body['crs'],
                      resampling=cov_resampling, src_nodata=None, dst_nodata=NODATA)
            mapped = []
            for r in range(shape[0]):
                row = []
                for c in range(shape[1]):
                    try:
                        row.append(map_cell(ds, transform_out, body['crs'], r, c))
                    except Exception:
                        row.append((None, None))
                mapped.append(row)
        layers.append((num, den, mapped))
    return layers


def _combine(body, layers):
    """按重叠策略合并候选源，返回结果栅格、逐格决定与逐源贡献证据。"""
    shape = (body['height'], body['width'])
    min_cov = body['min_coverage']
    method = body['method']
    policy = body['overlap_policy']
    result = np.full(shape, NODATA, dtype='float64')
    cells = []
    contribs = []
    for r in range(shape[0]):
        for c in range(shape[1]):
            mine = []
            for pos, (num, den, mapped) in enumerate(layers):
                raw = float(den[r, c])
                cov = raw if raw > 0 else 0.0
                ok = cov >= min_cov and cov > 0
                value = None
                if ok:
                    value = float(num[r, c]) if method == 'area' else float(num[r, c] / den[r, c])
                sr, sc = mapped[r][c]
                mine.append({'row': r, 'col': c, 'position': pos,
                             'stable_id': body['sources'][pos]['id'],
                             'source_hash': body['sources'][pos]['sha256'],
                             'coverage': cov, 'candidate': int(ok), 'value': value,
                             'weight': None, 'source_row': sr, 'source_col': sc})
            cand = [x['position'] for x in mine if x['candidate']]
            if not cand:
                decision, value = 'gap', None
            elif len(cand) == 1:
                decision = 'single'
                mine[cand[0]]['weight'] = 1.0
                value = mine[cand[0]]['value']
            elif policy == 'priority':
                decision = 'overlap'
                for k, pos in enumerate(cand):
                    mine[pos]['weight'] = 1.0 if k == 0 else 0.0
                value = mine[cand[0]]['value']
            else:
                decision = 'overlap'
                total = sum(mine[pos]['coverage'] for pos in cand)
                value = 0.0
                for pos in cand:
                    mine[pos]['weight'] = mine[pos]['coverage'] / total
                    value += mine[pos]['weight'] * mine[pos]['value']
            if value is not None:
                result[r, c] = value
            cells.append({'row': r, 'col': c, 'value': value, 'decision': decision})
            contribs.extend(mine)
    return result, cells, contribs


def run(db_path, contract, out):
    """执行多源拼接运行：冻结契约，逐源重投，按策略合并，事务化发布证据与输出。"""
    out = Path(out)
    mh = freeze(db_path, contract)
    db = connect(db_path)
    mid = db.execute('SELECT id FROM mosaic WHERE hash=?', (mh,)).fetchone()['id']
    body = json.loads(db.execute('SELECT body FROM mosaic WHERE id=?', (mid,)).fetchone()['body'])
    layers = _layers(body)
    result, cells, contribs = _combine(body, layers)
    transform_out = from_bounds(*body['bounds'], body['width'], body['height'])
    profile = {'driver': 'GTiff', 'width': body['width'], 'height': body['height'], 'count': 1,
               'dtype': 'float64', 'crs': body['crs'], 'transform': transform_out, 'nodata': NODATA}
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix('.tmp.tif')
    if out.exists():
        db.close()
        raise ValueError('output_exists')
    with rasterio.open(tmp, 'w', **profile) as dst:
        dst.write(result, 1)
    published = False
    try:
        with db:
            rid = db.execute("INSERT INTO mosaic_run(mosaic_id,status,method) VALUES (?,?,?)",
                             (mid, '采样中', body['method'])).lastrowid
            db.executemany('INSERT INTO mosaic_cell VALUES (?,?,?,?,?)',
                           [(rid, x['row'], x['col'], x['value'], x['decision']) for x in cells])
            db.executemany('INSERT INTO mosaic_contrib VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           [(rid, x['row'], x['col'], x['position'], x['stable_id'],
                             x['source_hash'], x['coverage'], x['candidate'], x['value'],
                             x['weight'], x['source_row'], x['source_col']) for x in contribs])
            for x in cells:
                if x['decision'] == 'gap':
                    db.execute('INSERT INTO mosaic_issue(run_id,row,col,code,detail) VALUES (?,?,?,?,?)',
                               (rid, x['row'], x['col'], 'no_source', 'no qualified source covers cell'))
            # 证据行有效后才暴露输出；发布失败回滚台账并清理产物。
            os.replace(tmp, out)
            published = True
            db.execute('INSERT INTO mosaic_output VALUES (?,?,?)', (rid, str(out), sha(out.read_bytes())))
            db.execute("UPDATE mosaic_run SET status=?,ended=CURRENT_TIMESTAMP WHERE id=?",
                       ('待核' if any(x['decision'] == 'gap' for x in cells) else '已核验', rid))
    except Exception:
        tmp.unlink(missing_ok=True)
        if published:
            out.unlink(missing_ok=True)
        raise
    finally:
        db.close()
    audit(db_path, rid)
    return rid


def _same_num(a, b):
    if a is None or b is None:
        return a is None and b is None
    return bool(np.isclose(a, b, rtol=1e-9, atol=1e-9))


def audit(db_path, rid):
    """独立复核：重读全部来源核验摘要，重算逐格贡献关系，比对输出与持久化证据。"""
    db = connect(db_path)
    row = db.execute('SELECT o.path AS out_path,o.hash AS out_hash,m.id AS mosaic_id,m.body '
                     'FROM mosaic_output o JOIN mosaic_run r ON r.id=o.run_id '
                     'JOIN mosaic m ON m.id=r.mosaic_id WHERE o.run_id=?', (rid,)).fetchone()
    if not row:
        db.close()
        raise ValueError('unknown_run')
    body = json.loads(row['body'])
    issues = []
    sources_ok = True
    registered = {x['position']: x for x in db.execute(
        'SELECT * FROM mosaic_source WHERE mosaic_id=? ORDER BY position', (row['mosaic_id'],))}
    for pos, src in enumerate(body['sources']):
        p = Path(src['path'])
        if not p.is_file() or sha(p.read_bytes()) != src['sha256']:
            issues.append('source_hash')
            sources_ok = False
            continue
        with rasterio.open(p) as ds:
            meta = (ds.width, ds.height, str(ds.crs), ds.nodata)
        if meta != (src['width'], src['height'], src['crs'], src['nodata']):
            issues.append('source_meta')
            sources_ok = False
        reg = registered.get(pos)
        if (reg is None or (reg['stable_id'], reg['hash'], reg['width'], reg['height'],
                            reg['crs'], reg['nodata'], reg['path'])
                != (src['id'], src['sha256'], src['width'], src['height'], src['crs'],
                    src['nodata'], src['path'])):
            issues.append('source_meta')
    p = Path(row['out_path'])
    output_ok = True
    if not p.is_file() or sha(p.read_bytes()) != row['out_hash']:
        issues.append('output_hash')
        output_ok = False
    n_cells = body['width'] * body['height']
    recorded_cells = [dict(x) for x in db.execute(
        'SELECT * FROM mosaic_cell WHERE run_id=? ORDER BY row,col', (rid,))]
    recorded_contribs = [dict(x) for x in db.execute(
        'SELECT * FROM mosaic_contrib WHERE run_id=? ORDER BY row,col,position', (rid,))]
    if len(recorded_cells) != n_cells:
        issues.append('cell_evidence_missing')
    if len(recorded_contribs) != n_cells * len(body['sources']):
        issues.append('contrib_evidence_missing')
    if sources_ok:
        _, cells, contribs = _combine(body, _layers(body))
        if len(recorded_cells) == n_cells:
            for want, got in zip(cells, recorded_cells):
                if (got['row'] != want['row'] or got['col'] != want['col']
                        or got['decision'] != want['decision']
                        or not _same_num(got['value'], want['value'])):
                    issues.append('cell_mismatch')
                    break
        if len(recorded_contribs) == len(contribs):
            for want, got in zip(contribs, recorded_contribs):
                if (got['row'] != want['row'] or got['col'] != want['col']
                        or got['position'] != want['position']
                        or got['stable_id'] != want['stable_id']
                        or got['source_hash'] != want['source_hash']
                        or got['candidate'] != want['candidate']
                        or not _same_num(got['coverage'], want['coverage'])
                        or not _same_num(got['value'], want['value'])
                        or not _same_num(got['weight'], want['weight'])
                        or not _same_num(got['source_row'], want['source_row'])
                        or not _same_num(got['source_col'], want['source_col'])):
                    issues.append('contrib_mismatch')
                    break
    if output_ok:
        with rasterio.open(p) as ds:
            if (ds.width != body['width'] or ds.height != body['height']
                    or str(ds.crs) != str(rasterio.crs.CRS.from_string(body['crs']))):
                issues.append('grid_mismatch')
            if not np.allclose(tuple(ds.bounds), tuple(body['bounds']), rtol=0, atol=1e-8):
                issues.append('bounds_mismatch')
            data = ds.read(1)
            finite = data[data != ds.nodata]
            if len(recorded_cells) == n_cells and any(
                    (got['value'] is None and data[got['row'], got['col']] != ds.nodata)
                    or (got['value'] is not None
                        and not np.isclose(data[got['row'], got['col']], got['value'],
                                           rtol=1e-9, atol=1e-9))
                    for got in recorded_cells):
                issues.append('cell_mismatch')
            if finite.size and not np.all(np.isfinite(finite)):
                issues.append('nonfinite')
    issues = list(dict.fromkeys(issues))
    with db:
        db.execute("DELETE FROM mosaic_issue WHERE run_id=? AND code LIKE 'audit_%'", (rid,))
        for code in issues:
            db.execute('INSERT INTO mosaic_issue(run_id,code,detail) VALUES (?,?,?)',
                       (rid, 'audit_' + code, 'independent mosaic verification'))
        if issues:
            state = '失败' if any(x in SEVERE for x in issues) else '待核'
            db.execute('UPDATE mosaic_run SET status=? WHERE id=?', (state, rid))
    db.close()
    return issues


def report(db_path, rid, path):
    """汇总运行：无源/单源/重叠格计数、按稳定格序的样例与逐格贡献证据。"""
    findings = audit(db_path, rid)
    db = connect(db_path)
    run_row = db.execute('SELECT * FROM mosaic_run WHERE id=?', (rid,)).fetchone()
    if not run_row:
        db.close()
        raise ValueError('unknown_run')
    body = json.loads(db.execute('SELECT body FROM mosaic WHERE id=?',
                                 (run_row['mosaic_id'],)).fetchone()['body'])
    transform_out = from_bounds(*body['bounds'], body['width'], body['height'])
    cells = []
    for row in db.execute('SELECT * FROM mosaic_cell WHERE run_id=? ORDER BY row,col', (rid,)):
        contribs = [dict(x) for x in db.execute(
            'SELECT position,stable_id,coverage,candidate,value,weight,source_row,source_col '
            'FROM mosaic_contrib WHERE run_id=? AND row=? AND col=? ORDER BY position',
            (rid, row['row'], row['col']))]
        x, y = transform_out @ (row['col'] + 0.5, row['row'] + 0.5)
        cells.append({'row': row['row'], 'col': row['col'], 'x': x, 'y': y,
                      'value': row['value'], 'decision': row['decision'], 'contribs': contribs})
    counts = {'gap': 0, 'single': 0, 'overlap': 0}
    for cell in cells:
        counts[cell['decision']] += 1
    samples = {k: [{'row': c['row'], 'col': c['col'], 'value': c['value'],
                    'sources': [x['stable_id'] for x in c['contribs'] if x['candidate']]}
                   for c in cells if c['decision'] == k][:5] for k in counts}
    obj = {'run': dict(run_row), 'mosaic': body,
           'output': dict(db.execute('SELECT * FROM mosaic_output WHERE run_id=?', (rid,)).fetchone()),
           'issues': [dict(x) for x in db.execute(
               'SELECT row,col,code,detail FROM mosaic_issue WHERE run_id=?', (rid,))],
           'audit': findings,
           'summary': {'gap': counts['gap'], 'single': counts['single'],
                       'overlap': counts['overlap'], 'samples': samples},
           'cells': cells}
    path = Path(path)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    path.with_suffix('.md').write_text(
        '# 多源拼接报告\n\n'
        f"运行：{run_row['status']}，输出复核：{obj['audit']}，"
        f"无源 {counts['gap']} 格，单源 {counts['single']} 格，重叠 {counts['overlap']} 格\n",
        encoding='utf-8')
    db.close()
    return path
