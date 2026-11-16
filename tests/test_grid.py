import json
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds
from gridreproject import engine

def source(tmp_path,data=None):
    path=tmp_path/'in.tif';data=np.array(data if data is not None else [[1.,2.],[3.,4.]])
    with rasterio.open(path,'w',driver='GTiff',width=2,height=2,count=1,dtype='float64',crs='EPSG:4326',transform=from_bounds(0,0,2,2,2,2),nodata=-9999) as dst:dst.write(data,1)
    return path
def cfg(**changes):
    obj={'crs':'EPSG:4326','bounds':[0,0,2,2],'width':2,'height':2,'kind':'continuous','method':'bilinear','min_coverage':1.0};obj.update(changes);return obj
def test_plan_and_method(tmp_path):
    assert engine.plan(tmp_path/'db',cfg())
    with pytest.raises(ValueError):engine.plan(tmp_path/'db',cfg(kind='category'))
def test_run_audit_report(tmp_path):
    rid=engine.run(tmp_path/'db',source(tmp_path),cfg(),tmp_path/'out.tif')
    assert engine.audit(tmp_path/'db',rid)==[]
    obj=json.loads(engine.report(tmp_path/'db',rid,tmp_path/'report.json').read_text(encoding='utf-8'))
    assert obj['run']['status']=='已核验'
def test_missing_and_tamper(tmp_path):
    rid=engine.run(tmp_path/'db',source(tmp_path,[[1,-9999],[3,4]]),cfg(),tmp_path/'out.tif')
    (tmp_path/'out.tif').write_bytes(b'bad')
    assert engine.audit(tmp_path/'db',rid)==['output_hash']
def test_invalid_grid(tmp_path):
    with pytest.raises(ValueError):engine.plan(tmp_path/'db',cfg(bounds=[0,0,0,2]))

def test_real_crs_transform(tmp_path):
    src=source(tmp_path);contract=cfg(crs='EPSG:3857',bounds=[0,0,222684,222684],min_coverage=0.2)
    rid=engine.run(tmp_path/'db',src,contract,tmp_path/'out.tif')
    assert engine.audit(tmp_path/'db',rid)==[]
    from gridreproject.store import connect
    db=connect(tmp_path/'db')
    rows=db.execute('SELECT source_row,source_col FROM cell WHERE run_id=?',(rid,)).fetchall()
    assert any(r['source_row'] is not None for r in rows)

def test_category_nearest_and_nodata(tmp_path):
    rid=engine.run(tmp_path/'db',source(tmp_path,[[1,-9999],[3,4]]),cfg(kind='category',method='nearest',min_coverage=1.0),tmp_path/'out.tif')
    from gridreproject.store import connect
    db=connect(tmp_path/'db')
    assert db.execute('SELECT count(*) FROM issue WHERE run_id=? AND code=?',(rid,'coverage_low')).fetchone()[0]>=1

def test_output_conflict_preserves_file(tmp_path):
    out=tmp_path/'out.tif';out.write_bytes(b'existing')
    with pytest.raises(ValueError,match='output_exists'):engine.run(tmp_path/'db',source(tmp_path),cfg(),out)
    assert out.read_bytes()==b'existing'

def test_source_tampering_detected(tmp_path):
    src=source(tmp_path);rid=engine.run(tmp_path/'db',src,cfg(),tmp_path/'out.tif')
    src.write_bytes(b'tampered')
    assert 'source_hash' in engine.audit(tmp_path/'db',rid)

def test_extensive_mode(tmp_path):
    rid=engine.run(tmp_path/'db',source(tmp_path),cfg(kind='extensive',method='area'),tmp_path/'out.tif')
    assert engine.audit(tmp_path/'db',rid)==[]

def test_extensive_downsample_conserves_total(tmp_path):
    rid=engine.run(tmp_path/'db',source(tmp_path),cfg(kind='extensive',method='area',width=1,height=1),tmp_path/'out.tif')
    with rasterio.open(tmp_path/'out.tif') as output:assert abs(float(output.read(1)[0,0])-10)<1e-6
    assert engine.audit(tmp_path/'db',rid)==[]

def test_outside_coverage_never_valid(tmp_path):
    rid=engine.run(tmp_path/'db',source(tmp_path),cfg(bounds=[100,100,102,102]),tmp_path/'out.tif')
    from gridreproject.store import connect
    db=connect(tmp_path/'db');assert db.execute('SELECT status FROM run WHERE id=?',(rid,)).fetchone()[0]=='待核'

def test_invalid_raster_rejected(tmp_path):
    p=tmp_path/'bad.tif';p.write_bytes(b'not a raster')
    with pytest.raises(Exception):engine.inspect(tmp_path/'db',p)

def test_report_has_cell_coverage_and_audit(tmp_path):
    rid=engine.run(tmp_path/'db',source(tmp_path),cfg(),tmp_path/'out.tif')
    obj=json.loads(engine.report(tmp_path/'db',rid,tmp_path/'r.json').read_text(encoding='utf-8'))
    assert len(obj['cells'])==4 and obj['audit']==[]
    assert all('coverage' in cell for cell in obj['cells'])

def test_extensive_audit_catches_wrong_total_even_with_updated_hash(tmp_path):
    rid=engine.run(tmp_path/'db',source(tmp_path),cfg(kind='extensive',method='area'),tmp_path/'out.tif')
    with rasterio.open(tmp_path/'out.tif') as old:
        profile=old.profile;data=old.read(1)
    data[0,0]+=5
    with rasterio.open(tmp_path/'out.tif','w',**profile) as changed:changed.write(data,1)
    from gridreproject.store import connect
    import hashlib
    db=connect(tmp_path/'db')
    with db:db.execute('UPDATE output SET hash=? WHERE run_id=?',(hashlib.sha256((tmp_path/'out.tif').read_bytes()).hexdigest(),rid))
    assert 'total_mismatch' in engine.audit(tmp_path/'db',rid)

def test_unknown_run_report_rejected(tmp_path):
    with pytest.raises(ValueError,match='unknown_run'):engine.report(tmp_path/'db',999,tmp_path/'r.json')
