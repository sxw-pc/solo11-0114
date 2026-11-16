import argparse,json,tempfile
from pathlib import Path
import numpy as np
import rasterio
from rasterio.transform import from_bounds
from . import engine

def demo():
    root=Path(tempfile.mkdtemp(prefix='gridreproject-'));src=root/'input.tif'
    with rasterio.open(src,'w',driver='GTiff',width=2,height=2,count=1,dtype='float64',crs='EPSG:4326',transform=from_bounds(0,0,2,2,2,2),nodata=-9999) as dst:dst.write(np.array([[1.,2.],[3.,4.]]),1)
    cfg={'crs':'EPSG:4326','bounds':[0,0,2,2],'width':2,'height':2,'kind':'continuous','method':'bilinear','min_coverage':1.0}
    rid=engine.run(root/'ledger.db',src,cfg,root/'out.tif');print(engine.report(root/'ledger.db',rid,root/'report.json'))
def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='cmd',required=True);sub.add_parser('demo')
    for name in ('inspect','plan','map','run','coverage','audit','report'):
        s=sub.add_parser(name);s.add_argument('--db',required=True)
        if name in ('inspect','run','map'):s.add_argument('--source',required=True)
        if name in ('plan','run','map'):s.add_argument('--contract',required=True)
        if name=='map':s.add_argument('--row',type=int,required=True);s.add_argument('--col',type=int,required=True)
        if name=='run':s.add_argument('--out',required=True)
        if name in ('coverage','audit','report'):s.add_argument('--run-id',type=int,required=True)
        if name=='report':s.add_argument('--path',required=True)
    x=p.parse_args()
    if x.cmd=='demo':demo()
    elif x.cmd=='inspect':print(engine.inspect(x.db,x.source))
    elif x.cmd=='plan':print(engine.plan(x.db,json.loads(Path(x.contract).read_text())))
    elif x.cmd=='map':
        from rasterio.transform import from_bounds
        with rasterio.open(x.source) as src:
            c=json.loads(Path(x.contract).read_text());print(engine.map_cell(src,from_bounds(*c['bounds'],c['width'],c['height']),c['crs'],x.row,x.col))
    elif x.cmd=='run':print(engine.run(x.db,x.source,json.loads(Path(x.contract).read_text()),x.out))
    elif x.cmd=='coverage':print(engine.coverage(x.db,x.run_id))
    elif x.cmd=='audit':print(engine.audit(x.db,x.run_id))
    else:print(engine.report(x.db,x.run_id,x.path))
if __name__=='__main__':main()
