import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds
from gridreproject import engine,operations
from gridreproject.store import connect

def run(tmp_path,data=None):
    src=tmp_path/'input.tif'
    values=np.array(data if data is not None else [[1.,2.],[3.,4.]])
    with rasterio.open(src,'w',driver='GTiff',width=2,height=2,count=1,dtype='float64',crs='EPSG:4326',transform=from_bounds(0,0,2,2,2,2),nodata=-9999) as dst:dst.write(values,1)
    db=tmp_path/'ledger.db'
    cfg={'crs':'EPSG:4326','bounds':[0,0,2,2],'width':2,'height':2,'kind':'continuous','method':'bilinear','min_coverage':0.5}
    rid=engine.run(db,src,cfg,tmp_path/'out.tif')
    return db,rid

def test_zonal_risk_and_release_are_separate_decisions(tmp_path):
    db,rid=run(tmp_path)
    operations.register_zone(db,'north',[[0,0],[0,1]],1.0,1.0)
    assert operations.evaluate_zone(db,rid,'north')['status']=='超限'
    assert operations.decide_release(db,'product',rid,['north'])['status']=='拒发'
    operations.register_zone(db,'south',[[1,0],[1,1]],1.0,10.0)
    assert operations.evaluate_zone(db,rid,'south')['status']=='正常'
    assert operations.decide_release(db,'another-product',rid,['south'])['status']=='准予发布'
    with pytest.raises(ValueError,match='release_identity_conflict'):
        operations.decide_release(db,'product',rid,['south'])

def test_coverage_gap_and_tampered_output_block_release(tmp_path):
    db,rid=run(tmp_path,[[1.,-9999.],[3.,4.]])
    operations.register_zone(db,'north',[[0,0],[0,1]],0.5,100.0)
    assert operations.evaluate_zone(db,rid,'north')['status']=='证据不足'
    assert operations.decide_release(db,'r1',rid,['north'])['status']=='暂缓'
    (tmp_path/'out.tif').write_bytes(b'tampered')
    assert operations.decide_release(db,'r2',rid,['north'])['status']=='暂缓'
    ledger=connect(db)
    assert ledger.execute("SELECT status FROM run WHERE id=?",(rid,)).fetchone()['status']!='已核验'

def test_zone_contract_is_immutable_and_invalid_cell_rejected(tmp_path):
    db,rid=run(tmp_path)
    operations.register_zone(db,'z',[[0,0]],1.0,5.0)
    with pytest.raises(ValueError,match='zone_version_conflict'):
        operations.register_zone(db,'z',[[0,0]],1.0,6.0)
    operations.register_zone(db,'outside',[[3,3]],1.0,5.0)
    with pytest.raises(ValueError,match='zone_cell_outside_grid'):
        operations.evaluate_zone(db,rid,'outside')
