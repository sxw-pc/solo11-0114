import argparse,json,tempfile
from pathlib import Path
import numpy as np
import rasterio
from rasterio.transform import from_bounds
from . import engine,operations,trend

def demo():
    root=Path(tempfile.mkdtemp(prefix='gridreproject-'));src=root/'input.tif'
    with rasterio.open(src,'w',driver='GTiff',width=2,height=2,count=1,dtype='float64',crs='EPSG:4326',transform=from_bounds(0,0,2,2,2,2),nodata=-9999) as dst:dst.write(np.array([[1.,2.],[3.,4.]]),1)
    cfg={'crs':'EPSG:4326','bounds':[0,0,2,2],'width':2,'height':2,'kind':'continuous','method':'bilinear','min_coverage':1.0}
    rid=engine.run(root/'ledger.db',src,cfg,root/'out.tif')
    operations.register_zone(root/'ledger.db','demo-zone',[[0,0],[0,1]],1.0,10.0)
    print(operations.evaluate_zone(root/'ledger.db',rid,'demo-zone'))
    print(operations.decide_release(root/'ledger.db','demo-release',rid,['demo-zone']))
    later=root/'later.tif'
    with rasterio.open(later,'w',driver='GTiff',width=2,height=2,count=1,dtype='float64',crs='EPSG:4326',transform=from_bounds(0,0,2,2,2,2),nodata=-9999) as dst:dst.write(np.array([[3.,0.],[3.,4.]]),1)
    later_id=engine.run(root/'ledger.db',later,cfg,root/'later-out.tif')
    trend.register(root/'ledger.db','demo-trend',rid,later_id,'demo-zone',1.0,1.0,0.0)
    print(trend.evaluate(root/'ledger.db','demo-trend'))
    print(engine.report(root/'ledger.db',rid,root/'report.json'))
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
    s=sub.add_parser('zone-register');s.add_argument('--db',required=True);s.add_argument('--name',required=True);s.add_argument('--cells',required=True);s.add_argument('--min-coverage',type=float,required=True);s.add_argument('--limit',type=float,required=True)
    s=sub.add_parser('zone-evaluate');s.add_argument('--db',required=True);s.add_argument('--run-id',type=int,required=True);s.add_argument('--name',required=True)
    s=sub.add_parser('release');s.add_argument('--db',required=True);s.add_argument('--run-id',type=int,required=True);s.add_argument('--request-id',required=True);s.add_argument('--zones',required=True)
    s=sub.add_parser('trend-register');s.add_argument('--db',required=True);s.add_argument('--id',required=True);s.add_argument('--baseline-run',type=int,required=True);s.add_argument('--target-run',type=int,required=True);s.add_argument('--zone',required=True);s.add_argument('--max-mean',type=float,required=True);s.add_argument('--max-cell',type=float,required=True);s.add_argument('--max-hotspot-fraction',type=float,required=True)
    s=sub.add_parser('trend-evaluate');s.add_argument('--db',required=True);s.add_argument('--id',required=True)
    s=sub.add_parser('contour-register');s.add_argument('--db',required=True);s.add_argument('--id',required=True);s.add_argument('--run-id',required=True,type=int);s.add_argument('--level',required=True,type=float);s.add_argument('--connectivity',required=True,choices=['low_connected','high_connected'])
    s=sub.add_parser('contour-segments');s.add_argument('--db',required=True);s.add_argument('--id',required=True)
    x=p.parse_args()
    if x.cmd=='demo':demo()
    elif x.cmd=='inspect':print(engine.inspect(x.db,x.source))
    elif x.cmd=='plan':print(engine.plan(x.db,json.loads(Path(x.contract).read_text())))
    elif x.cmd=='map':
        from rasterio.transform import from_bounds
        with rasterio.open(x.source) as src:
            c=json.loads(Path(x.contract).read_text());print(engine.map_cell(src,from_bounds(*c['bounds'],c['width'],c['height']),c['crs'],x.row,x.col))
    elif x.cmd=='run':print(engine.run(x.db,x.source,json.loads(Path(x.contract).read_text()),x.out))
    elif x.cmd=='zone-register':operations.register_zone(x.db,x.name,json.loads(Path(x.cells).read_text()),x.min_coverage,x.limit)
    elif x.cmd=='zone-evaluate':print(operations.evaluate_zone(x.db,x.run_id,x.name))
    elif x.cmd=='release':print(operations.decide_release(x.db,x.request_id,x.run_id,x.zones.split(',')))
    elif x.cmd=='trend-register':trend.register(x.db,x.id,x.baseline_run,x.target_run,x.zone,x.max_mean,x.max_cell,x.max_hotspot_fraction)
    elif x.cmd=='trend-evaluate':print(trend.evaluate(x.db,x.id))
    elif x.cmd=='contour-register':
        from .contour import register
        print(register(x.db,x.id,x.run_id,x.level,x.connectivity))
    elif x.cmd=='contour-segments':
        from .contour import segments
        print(segments(x.db,x.id))
    elif x.cmd=='coverage':print(engine.coverage(x.db,x.run_id))
    elif x.cmd=='audit':print(engine.audit(x.db,x.run_id))
    else:print(engine.report(x.db,x.run_id,x.path))
if __name__=='__main__':main()
