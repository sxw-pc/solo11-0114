# 栅格重投台设计

`source` 保存源栅格路径、内容摘要、CRS、尺寸、NoData 与单位；`contract` 冻结目标 CRS、范围、像元尺寸、变量语义和覆盖阈值。`run` 关联二者，`cell` 记录目标格的值、覆盖权重与逆映射源坐标，`issue` 标记覆盖不足，`output` 保存目标 GeoTIFF 路径及摘要。

运行状态为采样中、已核验或待核；输入或参数变化会产生新运行，已存在输出不能被覆盖。类别变量只可最近邻，连续变量只可双线性，广延量用 GDAL sum 重采样并校验总量。NoData 与数值零严格分开；低于有效覆盖阈值的目标格写 NoData。`bad_source_grid`、`bad_grid`、`method_kind_conflict`、`output_exists` 阻断执行；来源或输出摘要变化由 `audit` 发现。

本机已验证 `rasterio 1.5.2`、`numpy 2.5.3` 可在断网时用于运行。`python -m gridreproject demo` 生成合成二维 GeoTIFF，输出目标 GeoTIFF、SQLite 台账、JSON/Markdown 报告。测试包含 EPSG:4326 到 EPSG:3857 真正重投、NoData、类别与广延量、总量守恒、源外覆盖、来源/输出篡改。

分区风险子系统用 `zone` 与 `zone_decision` 固化格集合、阈值和裁定；产品发布子系统用 `release_request` 保存范围与结论。发布同时复核来源及输出、读取分区决定：超限拒发，缺格或证据不足暂缓，完整且合规则准予发布。

跨期变化子系统用 `trend` 冻结两次运行和变化预算，`trend_cell` 保留逐格基线、目标、覆盖率与热点证据。评估前重审两侧输出及契约一致性；平均值与热点比例分开裁定，覆盖缺口和篡改使比较不可用。

多源拼接子系统用 `mosaic` 冻结契约体（目标网格、变量语义、min_coverage、overlap_policy 与有序源及其字节哈希、地理元数据），`mosaic_source` 按位置镜像登记；契约哈希即运行身份，策略、顺序、门槛或来源内容变化都会改变它。`mosaic_run`、`mosaic_cell`、`mosaic_contrib` 持久化逐格决定（gap/single/overlap）与逐源覆盖、候选、权重和逆映射坐标；`mosaic_output` 保存输出摘要。priority 取契约顺序首个覆盖达标源，quality_weighted 只对连续量按有效覆盖归一加权；类别量与广延量用 quality_weighted 会在契约期被 `policy_kind_conflict` 拒绝。独立复核重读全部来源、重算贡献关系并比对输出与证据，来源、输出或贡献证据被篡改都会把运行降为失败；发布与证据提交在同一事务，失败回滚且不触碰已核验的单源输出。
