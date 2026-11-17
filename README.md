# 栅格重投台

离线单波段栅格重投影与覆盖审计。源登记、目标网格契约、逆映射、变量语义采样、缺测传播、覆盖诊断、输出再验和溯源报告构成完整流水线。运行期使用已安装 rasterio 1.5.2 与 numpy 2.5.3；`python -c "import rasterio,numpy; from rasterio.warp import transform"` 可自检。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m gridreproject demo`，`python -m gridreproject --help`，`python -m pytest -q`。运行与测试使用本地合成数据，不访问网络。

## 覆盖与运行结论

广延量的数值用面积汇总，覆盖率单独以有效像元比例计算；四格合一时即使三格的数值总和正确，缺失第四格也不能视为满覆盖。独立 `audit` 会复核源与输出哈希、网格和总量，发现问题写入 Issue 并将 Run 降为待核或失败。输出发布异常回滚 Run 与证据，并清除临时产物。

输出核验还逐格比对运行时持久化证据，并核对目标网格边界；即使输出哈希被同步改写，业务值或地理边界变化也会使 Run 失败。

## 分区风险与产品发布

`zone-register` 冻结目标格分区与阈值，`zone-evaluate` 根据持久化格值及覆盖证据裁定正常、超限或证据不足。`release` 将分区结论和运行审计合并成准予发布、拒发或暂缓的业务决定。相同栅格可因分区约束不同形成不同发布结果；NoData 或输出篡改不会被放行。

`trend-register` 固化两次运行、分区及变化预算，`trend-evaluate` 双边审计后逐格存储变化证据。均值未变但局部热点超限仍判恶化；改善、稳定、覆盖缺口和输出篡改各有不同结论。

`corridor-register` 冻结栅格通道端点、成本预算与共同失效格组；`corridor-search` 当前只在已审计连续栅格上求单条最低成本四邻接路径，尚不评估双路径冗余或共同失效组。
