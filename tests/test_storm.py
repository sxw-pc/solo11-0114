import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from gridreproject import engine, storm
from gridreproject.store import connect


def terrain_run(tmp_path):
    source = tmp_path/'terrain.tif'
    with rasterio.open(source, 'w', driver='GTiff', width=2, height=1,
                       count=1, dtype='float64', crs='EPSG:6933',
                       transform=from_bounds(0, 0, 2, 1, 2, 1), nodata=-9999) as dst:
        dst.write(np.array([[1., 0.]]), 1)
    db = tmp_path/'ledger.db'
    contract = {'crs':'EPSG:6933','bounds':[0,0,2,1],'width':2,'height':1,
                'kind':'continuous','method':'bilinear','min_coverage':1.0}
    return db, engine.run(db, source, contract, tmp_path/'terrain-output.tif')


def test_register_storm_for_terrain_run(tmp_path):
    db, run = terrain_run(tmp_path)
    event = Path(__file__).resolve().parents[1]/'fixtures'/'storm-event.json'
    result = storm.register(db, 'storm-1', run, event)
    assert result == storm.register(db, 'storm-1', run, event)
    with connect(db) as con:
        assert con.execute('SELECT count(*) FROM storm_contract').fetchone()[0] == 1


def test_storm_contract_checks_outlet_and_identity(tmp_path):
    db, run = terrain_run(tmp_path)
    event = tmp_path/'event.json'
    event.write_text('{"rainfall_mm":10,"outlets":[{"id":"gate","row":0,"col":1,"warning_m3":0.01}]}')
    storm.register(db, 'storm-1', run, event)
    event.write_text('{"rainfall_mm":10,"outlets":[{"id":"gate","row":2,"col":1,"warning_m3":0.01}]}')
    with pytest.raises(ValueError, match='outlet_outside_terrain'):
        storm.register(db, 'storm-2', run, event)
    event.write_text('{"rainfall_mm":20,"outlets":[{"id":"gate","row":0,"col":1,"warning_m3":0.01}]}')
    with pytest.raises(ValueError, match='storm_identity_conflict'):
        storm.register(db, 'storm-1', run, event)
