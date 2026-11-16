# 栅格重投台设计

`source` 保存源栅格路径、内容摘要、CRS、尺寸、NoData 与单位；`contract` 冻结目标 CRS、范围、像元尺寸、变量语义和覆盖阈值。`run` 关联二者，`cell` 记录目标格的值、覆盖权重与逆映射源坐标，`issue` 标记覆盖不足，`output` 保存目标 GeoTIFF 路径及摘要。

运行状态为采样中、已核验或待核；输入或参数变化会产生新运行，已存在输出不能被覆盖。类别变量只可最近邻，连续变量只可双线性，广延量用 GDAL sum 重采样并校验总量。NoData 与数值零严格分开；低于有效覆盖阈值的目标格写 NoData。`bad_source_grid`、`bad_grid`、`method_kind_conflict`、`output_exists` 阻断执行；来源或输出摘要变化由 `audit` 发现。

本机已验证 `rasterio 1.5.2`、`numpy 2.5.3` 可在断网时用于运行。`python -m gridreproject demo` 生成合成二维 GeoTIFF，输出目标 GeoTIFF、SQLite 台账、JSON/Markdown 报告。测试包含 EPSG:4326 到 EPSG:3857 真正重投、NoData、类别与广延量、总量守恒、源外覆盖、来源/输出篡改。

分区风险子系统用 `zone` 与 `zone_decision` 固化格集合、阈值和裁定；产品发布子系统用 `release_request` 保存范围与结论。发布同时复核来源及输出、读取分区决定：超限拒发，缺格或证据不足暂缓，完整且合规则准予发布。
