"""Freeze a paired local vector-field source without changing scalar reprojection."""
import hashlib
import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine

from .store import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS vector_source(
 id TEXT PRIMARY KEY,u_path TEXT NOT NULL,v_path TEXT NOT NULL,
 u_hash TEXT NOT NULL,v_hash TEXT NOT NULL,crs TEXT NOT NULL,
 transform_json TEXT NOT NULL,width INTEGER NOT NULL,height INTEGER NOT NULL,
 nodata REAL,basis TEXT NOT NULL);
"""

def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def _metadata(path):
    with rasterio.open(path) as src:
        if src.count != 1 or not src.crs or src.width <= 0 or src.height <= 0 or abs(src.transform.determinant) < 1e-15:
            raise ValueError('invalid_vector_component')
        return (str(src.crs), tuple(src.transform)[:6], src.width, src.height, src.nodata)

def register(db_path,vector_id,u_path,v_path,basis):
    if not vector_id or basis not in {'source_grid','earth_east_north'}:
        raise ValueError('invalid_vector_identity_or_basis')
    u_path,v_path=Path(u_path).resolve(),Path(v_path).resolve()
    if u_path==v_path:
        raise ValueError('vector_components_must_differ')
    u_meta,v_meta=_metadata(u_path),_metadata(v_path)
    if u_meta!=v_meta:
        raise ValueError('vector_component_grid_mismatch')
    crs,transform,width,height,nodata=u_meta
    row=(vector_id,str(u_path),str(v_path),_hash(u_path),_hash(v_path),crs,
         json.dumps(transform),width,height,nodata,basis)
    db=connect(db_path)
    try:
        db.executescript(SCHEMA)
        old=db.execute('SELECT * FROM vector_source WHERE id=?',(vector_id,)).fetchone()
        if old:
            if tuple(old)!=row:
                raise ValueError('vector_identity_conflict')
            return dict(old)
        with db:
            db.execute('INSERT INTO vector_source VALUES (?,?,?,?,?,?,?,?,?,?,?)',row)
        return inspect(db_path,vector_id)
    finally:
        db.close()

def inspect(db_path,vector_id):
    db=connect(db_path)
    try:
        db.executescript(SCHEMA)
        row=db.execute('SELECT * FROM vector_source WHERE id=?',(vector_id,)).fetchone()
        if not row:
            raise ValueError('unknown_vector')
        result=dict(row)
        result['intact']=(_hash(row['u_path'])==row['u_hash'] and _hash(row['v_path'])==row['v_hash']
                          and _metadata(row['u_path'])==_metadata(row['v_path']))
        return result
    finally:
        db.close()

def demo(db_path,workdir,fixture_path):
    fixture=json.loads(Path(fixture_path).read_text(encoding='utf-8'))
    root=Path(workdir).resolve();root.mkdir(parents=True,exist_ok=True)
    transform=Affine(*fixture['transform'])
    paths=[]
    for name in ('u','v'):
        path=root/(name+'.tif')
        if path.exists():
            raise ValueError('vector_demo_output_exists')
        data=np.asarray(fixture[name],dtype='float64')
        with rasterio.open(path,'w',driver='GTiff',width=data.shape[1],height=data.shape[0],
                           count=1,dtype='float64',crs=fixture['crs'],transform=transform,
                           nodata=fixture['nodata']) as dst:
            dst.write(data,1)
        paths.append(path)
    return register(db_path,fixture['id'],*paths,fixture['basis'])
