import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from gridreproject import control


FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'control-observations.json'


def source(tmp_path):
    raster = tmp_path / 'source.tif'
    with rasterio.open(raster, 'w', driver='GTiff', width=4, height=4, count=1,
                       dtype='float64', crs='EPSG:4326',
                       transform=from_bounds(0, 0, 4, 4, 4, 4), nodata=-9999) as dst:
        dst.write(np.arange(16, dtype='float64').reshape(4, 4), 1)
    return raster


def test_survey_registration_is_immutable(tmp_path):
    raster = source(tmp_path)
    db = tmp_path / 'ledger.db'
    assert control.register(db, 'survey-1', raster, FIXTURE) == 4
    assert control.register(db, 'survey-1', raster, FIXTURE) == 0
    assert len(control.inspect_survey(db, 'survey-1')['points']) == 4
    changed = json.loads(FIXTURE.read_text())
    changed['points'][0]['target_x'] = 0.6
    other = tmp_path / 'changed.json'
    other.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='identity_conflict'):
        control.register(db, 'survey-1', raster, other)


def test_duplicate_pixel_is_rejected(tmp_path):
    raster = source(tmp_path)
    body = json.loads(FIXTURE.read_text())
    body['points'][1]['source_col'] = body['points'][0]['source_col']
    other = tmp_path / 'duplicate.json'
    other.write_text(json.dumps(body))
    with pytest.raises(ValueError, match='duplicate'):
        control.register(tmp_path / 'ledger.db', 'survey-1', raster, other)
