# 测量误差区间与分区发布裁定

`run` 契约可选声明 `error_model`。未声明的运行完全沿用原有行为：`cell.value` 为名义值、`coverage` 为有效权重，分区按无权均值对 `limit_value` 裁定。声明后，每个有值目标格额外得到 `[value_lower, value_upper]` 与来源权重证据，分区裁定必须以区间为依据。

## 运行契约

```json
{
  "crs": "EPSG:4326", "bounds": [0, 0, 2, 2], "width": 2, "height": 2,
  "kind": "continuous", "method": "bilinear", "min_coverage": 1.0,
  "error_model": {"version": "m1", "abs_bound": 0.5}
}
```

或使用与源栅格同网格的本地误差栅格：

```json
"error_model": {"version": "local-1", "error_grid": "/path/to/error.tif"}
```

- `version` 必填，字符串，随运行冻结。
- `abs_bound`：每个有效源像元的非负绝对误差上界；`0` 合法。
- `error_grid`：误差栅格，必须与源栅格同 CRS、尺寸和仿射变换。有效像元给出该像元的非负绝对误差上界；NoData 表示**未知**，绝不当作零。
- 两者恰选其一；未知键、负数、非有限值、类别量 `kind=category` 一律拒绝（`bad_error_model` / `error_model_kind_conflict`）。
- 误差栅格缺失或不可读为 `error_grid_unreadable`；网格不一致为 `error_grid_mismatch`；存在负误差像元为 `error_grid_negative`。

运行时在 `run` 上冻结 `error_model_version`、`error_model`（规范化 JSON）、`error_hash`、`error_grid`、`error_grid_relation`。`error_hash` 对统一模型是规范化声明的 SHA-256，对本地误差栅格是文件字节的 SHA-256；`error_grid_relation` 冻结 CRS、宽高、仿射与 NoData。源栅格哈希仍由既有 `source.hash` 冻结。

## 逐格区间

设目标格的名义数值为 `v`。误差上界按与其数值相同的有效权重和归一化方式传播：

- **双线性（连续量）**：GDAL 以有效权重归一化 `num/den`。误差上界同样计算为 `sum(w_i * e_i) / den`，其中 `w_i` 为该格双线性模板中真正参与归一的有效邻元权重。无效（NoData）邻元不参与；它若被排除，就不贡献误差。若某个被加权的邻元误差未知，该目标格的区间记为未知（`error_known = 0`，上下界为 NULL），并在 `issue` 写入 `error_unknown`。仅当未知权重可按浮点容差视为 0 时才算已知，避免把"未参与"误判为"缺证据"。
- **面积权重（广延量）**：数值按 GDAL sum 的面积权重累计，误差上界按同一面积权重累计，即 `sum(a_i * e_i)`。`coverage` 仍单独由有效像元面积比例平均得到，两者语义不混用。
- 类别量拒绝数值误差模型。

缺失误差栅格、误差栅格被改动、逆映射失败或误差变换失败，都会使目标格区间未知；名义值正常不足以放行。逆映射失败同样把该格标为区间未知。

每格 `cell.weight_evidence` 以 JSON 保存 `mode`（`normalised_bilinear` / `area`）、`weight_sum`（参与数值归一的总权重）、`coverage_weight`、`error_weight_sum`、`unknown_weight_sum`、`bound` 以及逆映射源坐标，用于独立复核。

## 分区契约

`register_zone(path, name, cells, min_coverage, limit_value, strategy=None, strategy_version=None)`：

- `strategy` 省略时为旧版名义均值行为，只可与未声明误差模型的运行配合；对声明了误差模型的运行，该策略永远返回 `证据不足`。
- `strategy='mean_bound'`：对分区内全部有效格的上下界分别求平均，得到 `[mean(lower), mean(upper)]`。
- `strategy='hotspot_bound'`：取最危险单格包络 `[max(lower), max(upper)]`。
- 非空 `strategy_version` 必填，随分区契约冻结；同名分区不得改变格集合、覆盖率阈值、限制值、策略或策略版本（`zone_version_conflict`）。

裁定规则（两种策略一致，作用于各自的统计量）：

| 条件 | 结论 |
| --- | --- |
| 运行未核验、覆盖率不足、任一名义值缺失、任一所必需格区间未知 | 证据不足 |
| 统计量上界 `<= limit_value` | 正常 |
| 统计量下界 `> limit_value` | 超限 |
| 下界 `<= limit_value <` 上界 | 证据不足 |

反例（声明的分区风险语义必须被保留）：同一分区两格区间 `[1,2]` 与 `[8,9]`，阈值 6。`mean_bound` 得 `[4.5, 5.5]`，上界不超过 6，判正常并可发布；`hotspot_bound` 得 `[8,9]`，下界超过 6，判超限并拒发。不能把热点结论替换为均值，也不能把未知区间当作安全。

`zone_decision` 冻结 `strategy`、`strategy_version`、`lower_bound`、`upper_bound`、`model_version`、`model_hash` 与逐格 `evidence`（含 `value`、`lower`、`upper`、`error_known`、来源权重与 `crosses_limit`）。

## 发布

`decide_release` 先独立复核运行与误差来源，再读取分区决定：

- 运行未核验或任一分区证据不足 / 未裁定：`暂缓`。
- 任一分区超限：`拒发`。
- 全部分区正常：`准予发布`，并记录跨分区保守包络 `[min(lower), max(upper)]`。

`release_request` 冻结 `policy`（模型版本、模型哈希、分区策略、策略版本、限制值、覆盖率阈值、格集合）。同一 `request_id` 的运行或分区范围变化、策略变化一律 `release_identity_conflict`；策略变更必须新建请求，已有请求不会被静默重写。

## 审计

`audit` 独立核对：

- 源栅格哈希、输出哈希、网格、边界与逐格名义值；
- 误差栅格存在且哈希未变、网格关系与冻结值一致；
- 恢复源与误差栅格后重算全部逐格区间，与持久化的 `value_lower` / `value_upper` / `error_known` / `weight_evidence` 对比（`interval_mismatch`）；
- 分区决策与发布记录中的上下界与逐格证据一致（`zone_evidence_mismatch`），冻结策略仍与分区契约一致，模型版本 / 哈希与运行一致（`zone_model_mismatch`、`release_policy_mismatch`）。

任一失败写入 `audit_<code>` Issue 并把运行降为 `失败`，同一运行的 `准予发布` 请求自动转为 `暂缓 / audit_failed`。因此误差文件或持久化区间被改动时，既不能维持"已核验"，也不能维持"准予发布"。

## 报告

`report` 的 JSON 含 `run`、`output`、`issues`、`audit`、`cells`、`zones`、`releases`。`zones[*].flagged_cells` 按格列出跨阈值（`crosses_limit`）或误差未知（`missing_error`）的证据；Markdown 报告给出对应摘要。所有报告仅描述合成网格与相对行列号，不展示真实地理数据。

## 兼容性

- 未声明 `error_model` 的运行：`cell` 的新列保持 NULL，`run` 的误差列保持 NULL，分区无需策略即按原均值语义裁定，旧测试与旧台账均保持原结论。
- 旧版 SQLite 台账由 `store.connect` 原地增列，不做破坏性迁移。
