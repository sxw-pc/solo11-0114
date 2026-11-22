# 栅格重投台

离线单波段栅格重投影与覆盖审计。源登记、目标网格契约、逆映射、变量语义采样、缺测传播、覆盖诊断、输出再验和溯源报告构成完整流水线。运行期使用已安装 rasterio 1.5.2 与 numpy 2.5.3；`python -c "import rasterio,numpy; from rasterio.warp import transform"` 可自检。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m gridreproject demo`，`python -m gridreproject --help`，`python -m pytest -q`。运行与测试使用本地合成数据，不访问网络。

## 覆盖与运行结论

广延量的数值用面积汇总，覆盖率单独以有效像元比例计算；四格合一时即使三格的数值总和正确，缺失第四格也不能视为满覆盖。独立 `audit` 会复核源与输出哈希、网格和总量，发现问题写入 Issue 并将 Run 降为待核或失败。输出发布异常回滚 Run 与证据，并清除临时产物。

输出核验还逐格比对运行时持久化证据，并核对目标网格边界；即使输出哈希被同步改写，业务值或地理边界变化也会使 Run 失败。

## 分区风险与产品发布

`zone-register` 冻结目标格分区与阈值，`zone-evaluate` 根据持久化格值及覆盖证据裁定正常、超限或证据不足。`release` 将分区结论和运行审计合并成准予发布、拒发或暂缓的业务决定。相同栅格可因分区约束不同形成不同发布结果；NoData 或输出篡改不会被放行。

`trend-register` 固化两次运行、分区及变化预算，`trend-evaluate` 双边审计后逐格存储变化证据。均值未变但局部热点超限仍判恶化；改善、稳定、覆盖缺口和输出篡改各有不同结论。

## 外业控制点登记

`control-register --db DB --id SURVEY --source source.tif --observations fixtures/control-observations.json` 登记某份源栅格的合成外业控制点及目标 CRS，冻结原始观测摘要。`control-show --db DB --id SURVEY` 查看点和来源。登记只保全观测事实，尚不拟合几何校正或改变现有重投影输出。

## 冻结控制点几何校正

当源 GeoTIFF 自带仿射定位与外业实测冲突时，运行契约可显式追加 `survey_id`、`correction_model`（`global_affine` 或 `local_mesh`）、`control_residual_limit` 与 `mesh_boundary_policy`（当前仅支持 `reject`）；CLI 对应 `run --survey-id ... --correction-model ... --control-residual-limit ...`。省略这些参数时输出与历史版本完全一致。

`global_affine` 用全部有效控制点做加权二维仿射拟合，刻画整体平移、旋转与尺度偏差；`local_mesh` 要求控制点在源像素坐标构成至少 2×2 矩形网格，在相邻四点内双线性插值局部位移，节点边界之外拒绝定位而不默默外推。矩阵退化、网格折叠、逆解不收敛或残差超限都会拒绝输出并把失败原因与逐点残差写入台账，绝不回退到源文件 transform。目标像元中心经逆定位后按类别/连续/广延变量语义采样，广延量用雅可比面积加权超采样并独立计算覆盖率；NoData、源外与不可定位格写 NoData/“不可定位”，不以边缘值填补。

`control_fit` 冻结模型系数或网格节点与逐点残差，`cell_basis` 冻结每格逆定位依据。`audit` 从冻结原始观测重新核对摘要、重新拟合、逐格重算逆定位并比对输出；观测、系数、cell 证据或输出被改动则已核验 run 降为失败。校正失败不产生 run/output，也不覆盖此前已核验运行。合成测试覆盖整体仿射偏移、同一目标格在两模型下落到不同源列并产生不同输出值、退化/折叠、残差超限、源哈希变更、CRS 不可转换、NoData/覆盖、越界拒绝与三类篡改复核。
