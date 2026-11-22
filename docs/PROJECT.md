# 栅格重投台设计

`source` 保存源栅格路径、内容摘要、CRS、尺寸、NoData 与单位；`contract` 冻结目标 CRS、范围、像元尺寸、变量语义和覆盖阈值。`run` 关联二者，`cell` 记录目标格的值、覆盖权重与逆映射源坐标，`issue` 标记覆盖不足，`output` 保存目标 GeoTIFF 路径及摘要。

运行状态为采样中、已核验或待核；输入或参数变化会产生新运行，已存在输出不能被覆盖。类别变量只可最近邻，连续变量只可双线性，广延量用 GDAL sum 重采样并校验总量。NoData 与数值零严格分开；低于有效覆盖阈值的目标格写 NoData。`bad_source_grid`、`bad_grid`、`method_kind_conflict`、`output_exists` 阻断执行；来源或输出摘要变化由 `audit` 发现。

本机已验证 `rasterio 1.5.2`、`numpy 2.5.3` 可在断网时用于运行。`python -m gridreproject demo` 生成合成二维 GeoTIFF，输出目标 GeoTIFF、SQLite 台账、JSON/Markdown 报告。测试包含 EPSG:4326 到 EPSG:3857 真正重投、NoData、类别与广延量、总量守恒、源外覆盖、来源/输出篡改。

分区风险子系统用 `zone` 与 `zone_decision` 固化格集合、阈值和裁定；产品发布子系统用 `release_request` 保存范围与结论。发布同时复核来源及输出、读取分区决定：超限拒发，缺格或证据不足暂缓，完整且合规则准予发布。

跨期变化子系统用 `trend` 冻结两次运行和变化预算，`trend_cell` 保留逐格基线、目标、覆盖率与热点证据。评估前重审两侧输出及契约一致性；平均值与热点比例分开裁定，覆盖缺口和篡改使比较不可用。

## 外业控制点校正

`control_survey`/`control_point` 只冻结某源字节哈希下的外业观测（源像素行列、实测目标坐标、权重、目标 CRS 与原始观测摘要）。运行契约可显式追加 `survey_id`、`correction_model`、`control_residual_limit` 与 `mesh_boundary_policy`；任一参数或控制点版本变化都会改变契约哈希，产生独立运行身份，旧 run 的 cell 与 output 证明不可复用。未给出这些键时输出路径与历史版本完全一致。

校正运行先校验控制点组的 `source_hash` 与本次源文件字节一致、控制点目标 CRS 可转换到契约 CRS、观测点数量与权重足以定模，然后只依据冻结观测拟合“源像素中心→目标地理坐标”的变换，绝不回退到源 GeoTIFF 自带 transform。两种显式模型：

- `global_affine`：全部有效控制点加权最小二乘二维仿射（平移/旋转/尺度）。设计矩阵秩不足判 `degenerate_control_matrix`，雅可比奇异判 `control_geometry_folded`；每点保留预测坐标、残差、权重与是否采纳，最大残差超限判 `residual_limit_exceeded` 并拒绝输出。
- `local_mesh`：控制点须在源像素坐标上构成至少 2×2 完整矩形网格（否则 `mesh_layout_invalid`），相邻四点构成双线性位移片；逐片检查雅可比行列式角点与驻点，定向反转或零面积判 `mesh_folded`。目标中心落在节点网格之外时按契约拒绝（`point_outside_mesh`/`inverse_left_quad`/`inverse_not_converged`），不外推、不取边缘值填补。

目标像元中心先变换到控制点目标 CRS，再对拟合模型做逆定位；类别最近邻、连续双线性、广延量按雅可比面积加权超采样（GDAL sum 语义，覆盖率独立计算），保留 NoData 与覆盖判定，逆定位失败写“不可定位”、有效覆盖不足写 NoData。`control_fit`/`control_fit_point` 冻结模型系数或网格节点、逐点残差与拒绝原因（含被拒绝的拟合尝试，`run_id` 为空且不产生输出），`cell_basis` 冻结每格目标坐标、逆定位源坐标、所属片/参数与不可定位原因。`audit` 从冻结原始观测重算摘要、重新拟合并逐格重算逆定位：观测、系数、残差、cell 证据或输出任一项被改，对应 run 降为失败（`control_observation_tampered`、`control_fit_tampered`、`cell_basis_mismatch`、`control_evidence_missing`）。校正失败不写入 run/output，也不覆盖此前已核验运行及其 GeoTIFF。
