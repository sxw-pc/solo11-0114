"""Synthetic fixtures for content-addressed archive tests.

The shared-source scenario is size-sensitive: the acceptance story needs one
~4 MiB source TIFF reused by two contract runs whose outputs are ~1 MiB each,
while the per-run cell evidence stays small enough that the byte budget is
dominated by the raster bytes. We therefore use small 64x64 / 32x32 grids and
pad the GeoTIFFs with a private GDAL tag. The padding is real on-disk content
(hashed, copied and tamper-checked like every other byte) but invisible to the
grid/audit semantics, which only read the raster band and geotransform.
"""
import logging
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds

from gridreproject import engine
from gridreproject.store import connect

SOURCE_TARGET = 4 * 1024 * 1024
OUTPUT_TARGET = 1024 * 1024
# Headroom so the iterative pad loop lands just under the nominal target.
PAD_MARGIN = 512


def _pad_to(path, target_bytes):
    """Grow a GeoTIFF with a private tag until it reaches target_bytes.

    Opening a TIFF in ``r+`` rewrites the whole file once, so the pad length
    cannot be derived from the untouched baseline in one shot. Iterate: each
    round measures the current size and tops the tag up by the exact delta;
    size changes shrink to zero once the file has settled. GDAL may emit a
    cosmetic BLOCKXSIZE warning while rewriting the enlarged stripped TIFF;
    silence only that logger, only for this fixture.
    """
    rio_logger = logging.getLogger('rasterio._env')
    saved = rio_logger.disabled
    rio_logger.disabled = True
    try:
        for _ in range(6):
            size = path.stat().st_size
            if size >= target_bytes:
                return size
            with rasterio.open(path, 'r+') as tif:
                existing = tif.tags().get('GRIDREPROJECT_PADDING', '')
                tif.update_tags(GRIDREPROJECT_PADDING=existing + 'p' * (target_bytes - size - 64))
    finally:
        rio_logger.disabled = saved
    final = path.stat().st_size
    if final < target_bytes:
        raise RuntimeError('padding did not converge: %d < %d' % (final, target_bytes))
    return final


def build_shared_source_scenario(tmp_path, seed=7):
    """Two different-contract runs over one shared ~4 MiB source TIFF.

    Returns a dict with the db path, source path, run ids and output paths.
    Both runs cover full 1.0-coverage sub-windows, so each reaches the
    engine's ``已核验`` state and audits clean independently.
    """
    tmp_path = Path(tmp_path)
    rng = np.random.default_rng(seed)
    source = tmp_path / 'shared-source.tif'
    data = rng.normal(10.0, 2.0, (64, 64)).astype('float32')
    with rasterio.open(source, 'w', driver='GTiff', width=64, height=64, count=1,
                       dtype='float32', crs='EPSG:4326',
                       transform=from_bounds(0, 0, 64, 64, 64, 64),
                       nodata=-9999.0) as dst:
        dst.write(data, 1)
    source_size = _pad_to(source, SOURCE_TARGET)

    db_path = tmp_path / 'ledger.db'
    contracts = [
        {'crs': 'EPSG:4326', 'bounds': [0, 0, 32, 32], 'width': 32, 'height': 32,
         'kind': 'continuous', 'method': 'bilinear', 'min_coverage': 1.0},
        {'crs': 'EPSG:4326', 'bounds': [32, 32, 64, 64], 'width': 32, 'height': 32,
         'kind': 'continuous', 'method': 'bilinear', 'min_coverage': 1.0},
    ]
    run_ids = []
    output_sizes = []
    for i, contract in enumerate(contracts):
        out = tmp_path / f'out-{i}.tif'
        rid = engine.run(db_path, source, contract, out)
        # Padding the output changes its bytes: re-hash the output row so the
        # ledger evidence matches the actual file the archive will seal.
        size = _pad_to(out, OUTPUT_TARGET)
        with connect(db_path) as db:
            db.execute('UPDATE output SET hash=? WHERE run_id=?',
                       (engine.sha(out.read_bytes()), rid))
        assert engine.audit(db_path, rid) == []
        run_ids.append(rid)
        output_sizes.append(size)
    return {
        'db_path': db_path,
        'source': source,
        'source_size': source_size,
        'run_ids': run_ids,
        'outputs': [tmp_path / 'out-0.tif', tmp_path / 'out-1.tif'],
        'output_sizes': output_sizes,
        'contracts': contracts,
    }


def dedup_budget(scenario):
    """A budget that fits one source + both outputs + manifest/evidence."""
    # Evidence/contract/manifest together are well under 256 KiB here.
    return scenario['source_size'] + sum(scenario['output_sizes']) + 256 * 1024


def per_run_budget_floor(scenario):
    """The cost a naive per-run copy scheme would require (two source copies)."""
    return 2 * scenario['source_size'] + sum(scenario['output_sizes'])
