import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds
from gridreproject import engine, archive


def test_basic_archive_bundle(tmp_path):
    source = tmp_path / 'source.tif'
    with rasterio.open(source, 'w', driver='GTiff', width=2, height=2, count=1,
                       dtype='float64', crs='EPSG:4326',
                       transform=from_bounds(0,0,2,2,2,2), nodata=-9999) as dst:
        dst.write(np.array([[1.,2.],[3.,4.]]), 1)
    contract = {'crs':'EPSG:4326', 'bounds':[0,0,2,2], 'width':2, 'height':2,
                'kind':'continuous', 'method':'bilinear', 'min_coverage':1.0}
    db = tmp_path / 'ledger.db'
    rid = engine.run(db, source, contract, tmp_path / 'out.tif')
    first = archive.request(db, 'arc-1', [rid], 'full_replay')
    assert archive.request(db, 'arc-1', [rid], 'full_replay') == first
    manifest = archive.export(db, 'arc-1', tmp_path / 'bundle')
    assert manifest['runs'][0]['source_file'] == f'run-{rid}-source.tif'
    assert (tmp_path / 'bundle' / 'manifest.json').is_file()
    with pytest.raises(ValueError, match='archive_request_conflict'):
        archive.request(db, 'arc-1', [rid], 'product_only')
