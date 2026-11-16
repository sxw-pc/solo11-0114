import hashlib
import json
import os
from pathlib import Path
import numpy as np
import rasterio
from rasterio.transform import Affine, from_bounds, rowcol
from rasterio.warp import transform, reproject, Resampling
from .store import connect

def sha(data):return hashlib.sha256(data).hexdigest()
def inspect(db_path,path,unit='unit'):
    path=Path(path);raw=path.read_bytes()
    with rasterio.open(path) as src:
        if not src.crs or src.width<=0 or src.height<=0 or abs(src.transform.determinant)<1e-15:raise ValueError('bad_source_grid')
        info={'width':src.width,'height':src.height,'crs':str(src.crs),'nodata':src.nodata,'unit':unit,'hash':sha(raw)}
    db=connect(db_path)
    with db:
        db.execute('INSERT OR IGNORE INTO source(path,hash,width,height,crs,nodata,unit) VALUES (?,?,?,?,?,?,?)',(str(path),info['hash'],info['width'],info['height'],info['crs'],info['nodata'],unit))
    db.close();return info

def plan(db_path,contract):
    needed={'crs','bounds','width','height','kind','method','min_coverage'}
    if not needed.issubset(contract):raise ValueError('missing_contract')
    x0,y0,x1,y1=contract['bounds'];w=int(contract['width']);h=int(contract['height'])
    if w<=0 or h<=0 or x1<=x0 or y1<=y0 or not 0<=contract['min_coverage']<=1:raise ValueError('bad_grid')
    methods={'category':{'nearest'},'continuous':{'bilinear'},'extensive':{'area'}}
    if contract['method'] not in methods.get(contract['kind'],set()):raise ValueError('method_kind_conflict')
    try:rasterio.crs.CRS.from_string(contract['crs'])
    except Exception as exc:raise ValueError('bad_crs') from exc
    body=json.dumps(contract,sort_keys=True,separators=(',',':'))
    db=connect(db_path)
    with db:db.execute('INSERT OR IGNORE INTO contract(hash,body) VALUES (?,?)',(sha(body.encode()),body))
    db.close();return sha(body.encode())

def map_cell(src,target_transform,target_crs,r,c):
    x,y=target_transform @ (c+0.5,r+0.5)
    xs,ys=transform(target_crs,src.crs,[x],[y]);sr,sc=rowcol(src.transform,xs[0],ys[0],op=float)
    return sr,sc

def run(db_path,source_path,contract,out):
    source_path=Path(source_path);out=Path(out);info=inspect(db_path,source_path);ch=plan(db_path,contract)
    db=connect(db_path)
    src_id=db.execute('SELECT id FROM source WHERE hash=?',(info['hash'],)).fetchone()['id']
    cid=db.execute('SELECT id FROM contract WHERE hash=?',(ch,)).fetchone()['id']
    transform_out=from_bounds(*contract['bounds'],contract['width'],contract['height'])
    method=contract['method'];resampling={'nearest':Resampling.nearest,'bilinear':Resampling.bilinear,'area':Resampling.sum}[method]
    with rasterio.open(source_path) as src:
        source=src.read(1).astype('float64');valid=np.isfinite(source)
        if src.nodata is not None:valid &= source!=src.nodata
        values=np.where(valid,source,0.0);weight=valid.astype('float64')
        shape=(contract['height'],contract['width']);num=np.zeros(shape,dtype='float64');den=np.zeros(shape,dtype='float64')
        reproject(values,num,src_transform=src.transform,src_crs=src.crs,dst_transform=transform_out,dst_crs=contract['crs'],resampling=resampling,src_nodata=None,dst_nodata=-9999)
        reproject(weight,den,src_transform=src.transform,src_crs=src.crs,dst_transform=transform_out,dst_crs=contract['crs'],resampling=resampling,src_nodata=None,dst_nodata=-9999)
        result=np.full(shape,-9999.0,dtype='float64');good=den>=contract['min_coverage'];good &= den>0
        result[good]=num[good] if method=='area' else num[good]/den[good]
        provenance=[]
        for r in range(shape[0]):
            for c in range(shape[1]):
                try:sr,sc=map_cell(src,transform_out,contract['crs'],r,c)
                except Exception:sr=sc=None
                provenance.append((r,c,float(result[r,c]) if good[r,c] else None,float(den[r,c]),sr,sc,float(den[r,c])))
        profile=src.profile.copy();profile.update(driver='GTiff',width=shape[1],height=shape[0],count=1,dtype='float64',crs=contract['crs'],transform=transform_out,nodata=-9999.0)
    out.parent.mkdir(parents=True,exist_ok=True);tmp=out.with_suffix('.tmp.tif')
    if out.exists():raise ValueError('output_exists')
    with rasterio.open(tmp,'w',**profile) as dst:dst.write(result,1)
    os.replace(tmp,out)
    with db:
        rid=db.execute("INSERT INTO run(source_id,contract_id,status,method) VALUES (?,?,?,?)",(src_id,cid,'采样中',method)).lastrowid
        db.executemany('INSERT INTO cell VALUES (?,?,?,?,?,?,?,?)',[(rid,*x) for x in provenance])
        for r,c,value,cov,sr,sc,weight in provenance:
            if value is None:db.execute('INSERT INTO issue(run_id,row,col,code,detail) VALUES (?,?,?,?,?)',(rid,r,c,'coverage_low',f'{cov:.3f}'))
        db.execute('INSERT INTO output VALUES (?,?,?)',(rid,str(out),sha(out.read_bytes())))
        db.execute("UPDATE run SET status=?,ended=CURRENT_TIMESTAMP WHERE id=?",('待核' if np.any(~good) else '已核验',rid))
    db.close();return rid

def audit(db_path,rid):
    db=connect(db_path);row=db.execute('SELECT o.path,o.hash,c.body,s.path AS source_path,s.hash AS source_hash FROM output o JOIN run r ON r.id=o.run_id JOIN contract c ON c.id=r.contract_id JOIN source s ON s.id=r.source_id WHERE o.run_id=?',(rid,)).fetchone()
    if not row:raise ValueError('unknown_run')
    p=Path(row['path']);contract=json.loads(row['body']);issues=[]
    source_path=Path(row['source_path'])
    if not source_path.is_file() or sha(source_path.read_bytes())!=row['source_hash']:issues.append('source_hash')
    if not p.is_file() or sha(p.read_bytes())!=row['hash']:issues.append('output_hash')
    else:
        with rasterio.open(p) as src:
            if src.width!=contract['width'] or src.height!=contract['height'] or str(src.crs)!=str(rasterio.crs.CRS.from_string(contract['crs'])):issues.append('grid_mismatch')
            data=src.read(1);finite=data[data!=src.nodata]
            if finite.size and not np.all(np.isfinite(finite)):issues.append('nonfinite')
            if contract['kind']=='extensive' and source_path.is_file() and 'source_hash' not in issues:
                with rasterio.open(source_path) as origin:
                    original=origin.read(1)
                    valid=original!=origin.nodata if origin.nodata is not None else np.isfinite(original)
                    expected=float(original[valid].sum());actual=float(finite.sum())
                    if abs(actual-expected)>max(1e-6,abs(expected)*float(contract.get('total_tolerance',0.01))):issues.append('total_mismatch')
    db.close();return issues

def coverage(db_path,rid):
    db=connect(db_path);rows=[dict(x) for x in db.execute('SELECT row,col,coverage FROM cell WHERE run_id=? ORDER BY row,col',(rid,))];db.close();return rows

def report(db_path,rid,path):
    db=connect(db_path);run_row=db.execute('SELECT * FROM run WHERE id=?',(rid,)).fetchone()
    if not run_row:raise ValueError('unknown_run')
    obj={'run':dict(run_row),'output':dict(db.execute('SELECT * FROM output WHERE run_id=?',(rid,)).fetchone()),'issues':[dict(x) for x in db.execute('SELECT row,col,code,detail FROM issue WHERE run_id=?',(rid,))],'audit':audit(db_path,rid),'cells':[dict(x) for x in db.execute('SELECT row,col,value,coverage,source_row,source_col FROM cell WHERE run_id=? ORDER BY row,col',(rid,))]}
    path=Path(path);path.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8');path.with_suffix('.md').write_text('# 栅格重投报告\n\n'+f"运行：{run_row['status']}，输出复核：{obj['audit']}\n",encoding='utf-8');db.close();return path
