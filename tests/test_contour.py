import numpy as np
import rasterio
from rasterio.transform import from_bounds
from gridreproject import engine, contour


def test_local_contour_segments(tmp_path):
    source = tmp_path / 'plane.tif'
    with rasterio.open(source, 'w', driver='GTiff', width=3, height=3, count=1,
                       dtype='float64', crs='EPSG:4326',
                       transform=from_bounds(0,0,3,3,3,3), nodata=-9999) as dst:
        dst.write(np.array([[0.,2.,4.],[0.,2.,4.],[0.,2.,4.]]), 1)
    contract = {'crs':'EPSG:4326', 'bounds':[0,0,3,3], 'width':3, 'height':3,
                'kind':'continuous', 'method':'bilinear', 'min_coverage':1.0}
    db = tmp_path / 'ledger.db'
    rid = engine.run(db, source, contract, tmp_path / 'plane-out.tif')
    assert contour.register(db, 'iso-1', rid, 1, 'low_connected') == {'id': 'iso-1'}
    result = contour.segments(db, 'iso-1')
    assert len(result['segments']) == 2 and result['unresolved_saddles'] == 0
