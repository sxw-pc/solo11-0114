"""Freeze synthetic ground-control observations, without fitting a correction."""
import hashlib
import json
import math
from pathlib import Path

import rasterio

from .engine import inspect
from .store import connect


def register(db_path, survey_id, source_path, observations_path):
    if not survey_id:
        raise ValueError('control_survey_id_required')
    raw = Path(observations_path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    body = json.loads(raw)
    if not isinstance(body, dict) or not isinstance(body.get('points'), list) or len(body['points']) < 4:
        raise ValueError('control_points_insufficient')
    target_crs = body.get('target_crs')
    try:
        rasterio.crs.CRS.from_string(target_crs)
    except Exception as exc:
        raise ValueError('control_target_crs_invalid') from exc
    info = inspect(db_path, source_path)
    points = []
    seen_ids = set()
    seen_pixels = set()
    for point in body['points']:
        if not isinstance(point, dict) or not isinstance(point.get('id'), str) or not point['id']:
            raise ValueError('control_point_identity_invalid')
        try:
            row, col, x, y, weight = [float(point[key]) for key in
                                      ('source_row', 'source_col', 'target_x', 'target_y', 'weight')]
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError('control_point_coordinate_invalid') from exc
        if (not all(math.isfinite(value) for value in (row, col, x, y, weight)) or
                weight <= 0 or not 0 <= row < info['height'] or not 0 <= col < info['width']):
            raise ValueError('control_point_coordinate_invalid')
        pixel = (row, col)
        if point['id'] in seen_ids or pixel in seen_pixels:
            raise ValueError('control_point_duplicate')
        seen_ids.add(point['id'])
        seen_pixels.add(pixel)
        points.append((survey_id, point['id'], row, col, x, y, weight))
    db = connect(db_path)
    try:
        with db:
            old = db.execute('SELECT source_hash,target_crs,digest FROM control_survey WHERE id=?',
                             (survey_id,)).fetchone()
            identity = (info['hash'], target_crs, digest)
            if old:
                if tuple(old) != identity:
                    raise ValueError('control_survey_identity_conflict')
                return 0
            db.execute('INSERT INTO control_survey VALUES (?,?,?,?,?)',
                       (survey_id, *identity, raw.decode('utf-8')))
            db.executemany('INSERT INTO control_point VALUES (?,?,?,?,?,?,?)', points)
        return len(points)
    finally:
        db.close()


def inspect_survey(db_path, survey_id):
    db = connect(db_path)
    try:
        survey = db.execute('SELECT id,source_hash,target_crs,digest FROM control_survey WHERE id=?',
                            (survey_id,)).fetchone()
        if not survey:
            raise ValueError('unknown_control_survey')
        return {'survey': dict(survey), 'points': [dict(row) for row in db.execute(
            'SELECT point_id,source_row,source_col,target_x,target_y,weight FROM control_point '
            'WHERE survey_id=? ORDER BY point_id', (survey_id,))]}
    finally:
        db.close()
