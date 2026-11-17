import numpy as np
import rasterio
from rasterio.transform import from_bounds
from gridreproject import engine, corridor


def test_basic_corridor_and_frozen_groups(tmp_path):
    source = tmp_path / 'cost.tif'
    with rasterio.open(source, 'w', driver='GTiff', width=3, height=3, count=1,
                       dtype='float64', crs='EPSG:4326',
                       transform=from_bounds(0, 0, 3, 3, 3, 3), nodata=-9999) as dst:
        dst.write(np.ones((3, 3), dtype='float64'), 1)
    contract = {'crs': 'EPSG:4326', 'bounds': [0,0,3,3], 'width': 3, 'height': 3,
                'kind': 'continuous', 'method': 'bilinear', 'min_coverage': 1.0}
    db = tmp_path / 'ledger.db'
    rid = engine.run(db, source, contract, tmp_path / 'cost-out.tif')
    assert corridor.register(db, 'route-1', rid, [0,0], [2,2], 5,
                             [[[1,1]]]) == {'id': 'route-1'}
    result = corridor.search(db, 'route-1')
    assert result['status'] == '可通行' and result['cost'] == 4
