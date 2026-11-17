from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from gridreproject import engine, sight
from gridreproject.store import connect


def terrain_run(tmp_path):
    source = tmp_path/'terrain.tif'
    with rasterio.open(source, 'w', driver='GTiff', width=2, height=1,
                       count=1, dtype='float64', crs='EPSG:6933',
                       transform=from_bounds(0, 0, 2, 1, 2, 1), nodata=-9999) as dst:
        dst.write(np.array([[1., 2.]]), 1)
    db = tmp_path/'ledger.db'
    contract = {'crs':'EPSG:6933','bounds':[0,0,2,1],'width':2,'height':1,
                'kind':'continuous','method':'bilinear','min_coverage':1.0}
    return db, engine.run(db, source, contract, tmp_path/'terrain-output.tif')


def test_register_sites_freezes_survey(tmp_path):
    db, run = terrain_run(tmp_path)
    sites = Path(__file__).resolve().parents[1]/'fixtures'/'survey-sites.json'
    result = sight.register(db, 'network-A', run, sites)
    assert result == sight.register(db, 'network-A', run, sites)
    assert result['site_count'] == 2
    with connect(db) as con:
        assert con.execute('SELECT count(*) FROM sight_network').fetchone()[0] == 1


def test_register_rejects_outside_or_changed_site(tmp_path):
    db, run = terrain_run(tmp_path)
    sites = tmp_path/'sites.json'
    sites.write_text('[{"id":"a","x":0.5,"y":0.5,"antenna_m":5,"max_range_m":10,"min_clearance_m":1}]')
    sight.register(db, 'network-A', run, sites)
    sites.write_text('[{"id":"a","x":2.5,"y":0.5,"antenna_m":5,"max_range_m":10,"min_clearance_m":1}]')
    with pytest.raises(ValueError, match='invalid_survey_site'):
        sight.register(db, 'network-B', run, sites)
    sites.write_text('[{"id":"a","x":0.5,"y":0.5,"antenna_m":6,"max_range_m":10,"min_clearance_m":1}]')
    with pytest.raises(ValueError, match='sight_network_conflict'):
        sight.register(db, 'network-A', run, sites)
