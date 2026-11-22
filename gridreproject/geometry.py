"""Frozen control-point geometry: fit, fold checks and inverse localization.

Two explicit geometric hypotheses are supported:

* ``global_affine`` — one weighted 2-D affine transform
  ``target_xy = M @ [col, row] + t`` fitted over *all* accepted control
  points. It describes a whole-raster translation/rotation/scale bias.
* ``local_mesh`` — the control points must form a rectangular grid of at
  least 2 x 2 nodes in source pixel coordinates; a bilinear patch inside
  each four-node quad gives a continuous local displacement field. No
  extrapolation outside the node grid is ever performed.

The source file's own GeoTIFF transform is never used inside this module:
every forward and inverse coordinate relation comes solely from the frozen
field observations.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import rasterio
from rasterio.warp import transform as warp_transform

REL_EPS = 1e-12
INVERSE_MAX_ITER = 32
INVERSE_TOL = 1e-11


class GeometryError(ValueError):
    """A geometry failure carrying a stable machine-readable code."""

    def __init__(self, code, detail='', payload=None):
        super().__init__(code if not detail else f'{code}: {detail}')
        self.code = code
        self.detail = detail
        self.payload = payload or {}


def ensure_crs_transformable(source_crs, target_crs):
    """Raise ``control_crs_not_transformable`` unless a CRS path exists."""
    try:
        src = rasterio.crs.CRS.from_user_input(source_crs)
        dst = rasterio.crs.CRS.from_user_input(target_crs)
        warp_transform(src, dst, [0.0, 1.0], [0.0, 1.0])
    except Exception as exc:  # pragma: no cover - rasterio raises varied types
        raise GeometryError('control_crs_not_transformable', str(exc)) from exc


def crs_transform(source_crs, target_crs, xs, ys):
    """Batch coordinate transform with finite-value validation."""
    out_x, out_y = warp_transform(source_crs, target_crs, list(xs), list(ys))
    if not (all(math.isfinite(v) for v in out_x) and
            all(math.isfinite(v) for v in out_y)):
        raise GeometryError('control_crs_not_transformable',
                            'non-finite transformed coordinate')
    return out_x, out_y


def _quadratic_roots(k2, k1, k0):
    """Real roots of ``k2 v**2 - k1 v - k0 = 0`` (stable formulation)."""
    if abs(k2) <= REL_EPS * (abs(k1) + abs(k0) + 1.0):
        if abs(k1) <= REL_EPS * (abs(k0) + 1.0):
            return []
        return [-k0 / k1]
    a, b, c = k2, -k1, -k0
    discriminant = b * b - 4 * a * c
    if discriminant < 0:
        return []
    root = math.sqrt(discriminant)
    q = -0.5 * (b + math.copysign(root, b))
    roots = [q / a]
    if abs(q) > 0:
        roots.append(c / q)
    return roots


def _point_arrays(points):
    rows = np.array([float(p['source_row']) for p in points], dtype='float64')
    cols = np.array([float(p['source_col']) for p in points], dtype='float64')
    xs = np.array([float(p['target_x']) for p in points], dtype='float64')
    ys = np.array([float(p['target_y']) for p in points], dtype='float64')
    w = np.array([float(p['weight']) for p in points], dtype='float64')
    return rows, cols, xs, ys, w


@dataclass(frozen=True)
class AffineModel:
    """target_x = ax*col + bx*row + cx (y analogously)."""

    ax: float
    bx: float
    cx: float
    ay: float
    by: float
    cy: float

    @property
    def determinant(self):
        return self.ax * self.by - self.bx * self.ay

    def forward(self, row, col):
        x = self.ax * col + self.bx * row + self.cx
        y = self.ay * col + self.by * row + self.cy
        return x, y

    def inverse(self, x, y):
        det = self.determinant
        if abs(det) <= REL_EPS * max(1.0, abs(self.ax * self.by),
                                     abs(self.bx * self.ay)):
            raise GeometryError('control_geometry_folded',
                                'affine determinant not invertible')
        dcx, dcy = x - self.cx, y - self.cy
        col = (self.by * dcx - self.bx * dcy) / det
        row = (-self.ay * dcx + self.ax * dcy) / det
        return row, col

    def jacobian(self, row, col):
        return abs(self.determinant)

    def to_dict(self):
        return {'model': 'global_affine',
                'coefficients': {'ax': self.ax, 'bx': self.bx, 'cx': self.cx,
                                 'ay': self.ay, 'by': self.by, 'cy': self.cy}}


def fit_global_affine(points, residual_limit):
    """Weighted least-squares affine fit with rank/fold/residual gates."""
    rows, cols, xs, ys, w = _point_arrays(points)
    design = np.column_stack([cols, rows, np.ones_like(cols)])
    weighted = design * w[:, None]
    rank = int(np.linalg.matrix_rank(weighted, tol=REL_EPS))
    if rank < 3 or len(points) < 3:
        raise GeometryError('degenerate_control_matrix',
                            f'rank={rank}, points={len(points)}')
    (ax, bx, cx), *_ = np.linalg.lstsq(weighted, xs * w, rcond=None)
    (ay, by, cy), *_ = np.linalg.lstsq(weighted, ys * w, rcond=None)
    model = AffineModel(float(ax), float(bx), float(cx),
                        float(ay), float(by), float(cy))
    linear = np.array([[model.ax, model.bx], [model.ay, model.by]])
    singular = np.linalg.svd(linear, compute_uv=False)
    if singular.min() < REL_EPS * singular.max() or not math.isfinite(model.determinant):
        raise GeometryError('control_geometry_folded',
                            f'singular values={singular.tolist()}')
    records = []
    worst = 0.0
    for point, row, col in zip(points, rows, cols):
        px, py = model.forward(row, col)
        residual = math.hypot(px - point['target_x'], py - point['target_y'])
        worst = max(worst, residual)
        records.append({'point_id': point['id'], 'residual': residual,
                        'predicted_x': px, 'predicted_y': py,
                        'accepted': residual <= residual_limit,
                        'reason': None if residual <= residual_limit
                        else 'residual_limit_exceeded'})
    if worst > residual_limit:
        raise GeometryError('residual_limit_exceeded',
                            f'max residual {worst:g} > limit {residual_limit:g}',
                            payload={'records': records, 'model': model})
    return model, records


@dataclass(frozen=True)
class MeshModel:
    """Bilinear control mesh indexed by sorted source pixel rows/cols."""

    rows: tuple
    cols: tuple
    nodes_x: tuple          # nodes_x[i][j] at (rows[i], cols[j])
    nodes_y: tuple
    max_iter: int = INVERSE_MAX_ITER

    def to_dict(self):
        return {'model': 'local_mesh',
                'rows': list(self.rows), 'cols': list(self.cols),
                'nodes': [[{'row': self.rows[i], 'col': self.cols[j],
                            'target_x': self.nodes_x[i][j],
                            'target_y': self.nodes_y[i][j]}
                           for j in range(len(self.cols))]
                          for i in range(len(self.rows))]}

    def _cell_index(self, values, coordinate):
        idx = int(np.searchsorted(values, coordinate, side='right') - 1)
        return min(max(idx, 0), len(values) - 2)

    def _patch(self, i, j):
        r0, r1 = self.rows[i], self.rows[i + 1]
        c0, c1 = self.cols[j], self.cols[j + 1]
        p00 = np.array([self.nodes_x[i][j], self.nodes_y[i][j]])
        p10 = np.array([self.nodes_x[i + 1][j], self.nodes_y[i + 1][j]])
        p01 = np.array([self.nodes_x[i][j + 1], self.nodes_y[i][j + 1]])
        p11 = np.array([self.nodes_x[i + 1][j + 1],
                        self.nodes_y[i + 1][j + 1]])
        return r0, r1, c0, c1, p00, p10, p01, p11

    def forward(self, row, col):
        if not (self.rows[0] <= row <= self.rows[-1] and
                self.cols[0] <= col <= self.cols[-1]):
            raise GeometryError('point_outside_mesh',
                                f'pixel ({row:g},{col:g}) outside node grid')
        i = self._cell_index(self.rows, row)
        j = self._cell_index(self.cols, col)
        r0, r1, c0, c1, p00, p10, p01, p11 = self._patch(i, j)
        u = (row - r0) / (r1 - r0)
        v = (col - c0) / (c1 - c0)
        point = (p00 + u * (p10 - p00) + v * (p01 - p00)
                 + u * v * (p11 - p10 - p01 + p00))
        return float(point[0]), float(point[1])

    def _jacobian(self, i, j, u, v):
        _, _, _, _, p00, p10, p01, p11 = self._patch(i, j)
        cross = p11 - p10 - p01 + p00
        du = (p10 - p00) + v * cross
        dv = (p01 - p00) + u * cross
        return float(du[0] * dv[1] - du[1] * dv[0])

    def _solve_quad(self, i, j, target):
        """Analytic bilinear inverse on one parameter square.

        Returns (u, v) in [0, 1]**2 or raises GeometryError. The two bilinear
        component equations reduce to a univariate quadratic; a damped Newton
        fallback covers the near-degenerate denominator cases.
        """
        r0, r1, c0, c1, p00, p10, p01, p11 = self._patch(i, j)
        a_vec = p10 - p00
        b_vec = p01 - p00
        c_vec = p11 - p10 - p01 + p00
        d_vec = target - p00
        ax, ay = a_vec
        bx, by = b_vec
        cx, cy = c_vec
        dx, dy = d_vec

        def det(p, q):
            return p[0] * q[1] - p[1] * q[0]

        k2 = det(b_vec, c_vec)
        k1 = det(a_vec, b_vec) + det(d_vec, c_vec)
        use_x = abs(ax) + abs(cx) >= abs(ay) + abs(cy)
        k0 = (ay * dx - ax * dy) if use_x else (ax * dy - ay * dx)
        roots = _quadratic_roots(k2, k1, k0)
        candidates = []
        for v in roots:
            if use_x:
                denom = ax + cx * v
                if abs(denom) < REL_EPS * (abs(ax) + abs(cx) + 1.0):
                    continue
                u = (dx - bx * v) / denom
            else:
                denom = ay + cy * v
                if abs(denom) < REL_EPS * (abs(ay) + abs(cy) + 1.0):
                    continue
                u = (dy - by * v) / denom
            point = (p00 + u * a_vec + v * b_vec + u * v * c_vec)
            residual = float(np.linalg.norm(target - point))
            candidates.append((residual, u, v))
        for residual, u, v in sorted(candidates):
            if (-1e-8 <= u <= 1 + 1e-8 and -1e-8 <= v <= 1 + 1e-8 and
                    residual <= INVERSE_TOL * (1.0 + float(np.linalg.norm(target)))):
                return min(max(u, 0.0), 1.0), min(max(v, 0.0), 1.0)
        return self._damped_newton(i, j, target, a_vec, b_vec, c_vec, p00)

    def _damped_newton(self, i, j, target, a_vec, b_vec, c_vec, p00):
        u = v = 0.5
        best_residual = float('inf')
        for _ in range(max(1, self.max_iter)):
            point = p00 + u * a_vec + v * b_vec + u * v * c_vec
            residual = target - point
            norm = float(np.linalg.norm(residual))
            if norm <= INVERSE_TOL * (1.0 + float(np.linalg.norm(target))):
                if -1e-8 <= u <= 1 + 1e-8 and -1e-8 <= v <= 1 + 1e-8:
                    return min(max(u, 0.0), 1.0), min(max(v, 0.0), 1.0)
                raise GeometryError('inverse_left_quad')
            du = a_vec + v * c_vec
            dv = b_vec + u * c_vec
            detv = float(du[0] * dv[1] - du[1] * dv[0])
            scale = max(1.0, np.linalg.norm(du), np.linalg.norm(dv))
            if abs(detv) < REL_EPS * scale * scale:
                raise GeometryError('mesh_folded')
            step_u = (residual[0] * dv[1] - residual[1] * dv[0]) / detv
            step_v = (du[0] * residual[1] - du[1] * residual[0]) / detv
            alpha = 1.0
            accepted = False
            for _ in range(60):
                nu, nv = u + alpha * step_u, v + alpha * step_v
                inside = (-1e-7 <= nu <= 1 + 1e-7 and -1e-7 <= nv <= 1 + 1e-7)
                npoint = p00 + nu * a_vec + nv * b_vec + nu * nv * c_vec
                nnorm = float(np.linalg.norm(target - npoint))
                if inside and nnorm < norm:
                    u, v, best_residual, accepted = nu, nv, nnorm, True
                    break
                alpha *= 0.5
            if not accepted:
                raise GeometryError('inverse_not_converged')
        raise GeometryError('inverse_not_converged')

    def inverse(self, x, y):
        target = np.array([x, y], dtype='float64')
        best_reason = 'point_outside_mesh'
        saw_candidate = False
        for i in range(len(self.rows) - 1):
            for j in range(len(self.cols) - 1):
                r0, r1, c0, c1, p00, p10, p01, p11 = self._patch(i, j)
                box = np.column_stack([p00, p10, p01, p11])
                span = float(np.ptp(box, axis=1).max()) + 1.0
                if (x < box[0].min() - REL_EPS * span or
                        x > box[0].max() + REL_EPS * span or
                        y < box[1].min() - REL_EPS * span or
                        y > box[1].max() + REL_EPS * span):
                    continue
                saw_candidate = True
                try:
                    u, v = self._solve_quad(i, j, target)
                except GeometryError as exc:
                    if exc.code in ('mesh_folded', 'inverse_left_quad',
                                    'inverse_not_converged'):
                        best_reason = exc.code
                    continue
                row = r0 + u * (r1 - r0)
                col = c0 + v * (c1 - c0)
                return (float(row), float(col), i, j, float(u), float(v))
        # Points beyond every quad bbox are outside the node hull by contract.
        if not saw_candidate:
            best_reason = 'point_outside_mesh'
        raise GeometryError(best_reason, f'target=({x:g},{y:g})')

    def jacobian(self, row, col):
        i = self._cell_index(self.rows, row)
        j = self._cell_index(self.cols, col)
        r0, r1, c0, c1 = self._patch(i, j)[:4]
        u = (row - r0) / (r1 - r0)
        v = (col - c0) / (c1 - c0)
        return abs(self._jacobian(i, j, u, v))


def _bilinear_determinant_corners(i, j, mesh):
    """Determinant of the patch Jacobian at the four parameter corners."""
    values = []
    for u in (0.0, 1.0):
        for v in (0.0, 1.0):
            values.append(mesh._jacobian(i, j, u, v))
    # order: (0,0),(0,1),(1,0),(1,1)
    d00, d01, d10, d11 = values[0], values[1], values[2], values[3]
    return d00, d01, d10, d11


def fit_local_mesh(points, residual_limit):
    """Rectangular 2x2+ node grid with per-quad fold protection."""
    rows = sorted({float(p['source_row']) for p in points})
    cols = sorted({float(p['source_col']) for p in points})
    if len(rows) < 2 or len(cols) < 2:
        raise GeometryError('mesh_layout_invalid',
                            f'{len(rows)} distinct rows, {len(cols)} cols')
    if (any(b - a <= REL_EPS for a, b in zip(rows, rows[1:])) or
            any(b - a <= REL_EPS for a, b in zip(cols, cols[1:]))):
        raise GeometryError('mesh_layout_invalid', 'zero node spacing')
    lookup = {(float(p['source_row']), float(p['source_col'])): p
              for p in points}
    if len(lookup) != len(points) or len(points) != len(rows) * len(cols):
        raise GeometryError('mesh_layout_invalid',
                            'control points do not form a full rectangular grid')
    nodes_x, nodes_y = [], []
    by_id = {}
    for r in rows:
        line_x, line_y = [], []
        for c in cols:
            point = lookup.get((r, c))
            if point is None:
                raise GeometryError('mesh_layout_invalid',
                                    f'missing node at row={r:g}, col={c:g}')
            line_x.append(float(point['target_x']))
            line_y.append(float(point['target_y']))
            by_id[point['id']] = (r, c, float(point['target_x']),
                                  float(point['target_y']))
        nodes_x.append(tuple(line_x))
        nodes_y.append(tuple(line_y))
    mesh = MeshModel(tuple(rows), tuple(cols), tuple(nodes_x),
                     tuple(nodes_y))
    # Fold detection: Jacobian determinant is bilinear in (u,v); its extrema
    # over the unit square are at the corners or the interior stationary point.
    for i in range(len(rows) - 1):
        for j in range(len(cols) - 1):
            d00, d01, d10, d11 = _bilinear_determinant_corners(i, j, mesh)
            corner_dets = [d00, d01, d10, d11]
            if any(d * d00 < 0 for d in corner_dets):
                raise GeometryError('mesh_folded',
                                    f'quad ({i},{j}) orientation reversal')
            a_coef = d10 - d00
            b_coef = d01 - d00
            c_coef = d11 - d10 - d01 + d00
            candidates = list(corner_dets)
            if abs(c_coef) > REL_EPS:
                u_star = -b_coef / c_coef
                v_star = -a_coef / c_coef
                if 0 < u_star < 1 and 0 < v_star < 1:
                    candidates.append(d00 + a_coef * u_star + b_coef * v_star
                                      + c_coef * u_star * v_star)
            _, r1, _, c1, p00, p10, p01, p11 = mesh._patch(i, j)
            edge_scale = max(1e-30,
                             np.linalg.norm(p10 - p00) * np.linalg.norm(p01 - p00))
            if min(abs(d) for d in candidates) < REL_EPS * edge_scale:
                raise GeometryError('mesh_folded',
                                    f'quad ({i},{j}) collapses to zero area')
    records = []
    for point in points:
        r, c, px, py = by_id[point['id']]
        residual = math.hypot(px - point['target_x'], py - point['target_y'])
        records.append({'point_id': point['id'], 'residual': residual,
                        'predicted_x': px, 'predicted_y': py,
                        'accepted': residual <= residual_limit,
                        'reason': None if residual <= residual_limit
                        else 'residual_limit_exceeded'})
    if any(not rec['accepted'] for rec in records):
        raise GeometryError('residual_limit_exceeded', payload={'records': records})
    return mesh, records


def fit_model(model_name, points, residual_limit):
    if model_name == 'global_affine':
        return fit_global_affine(points, residual_limit)
    if model_name == 'local_mesh':
        return fit_local_mesh(points, residual_limit)
    raise GeometryError('unknown_correction_model', model_name)
