# 栅格重投台

离线单波段栅格重投影与覆盖审计。源登记、目标网格契约、逆映射、变量语义采样、缺测传播、覆盖诊断、输出再验和溯源报告构成完整流水线。运行期使用已安装 rasterio 1.5.2 与 numpy 2.5.3；`python -c "import rasterio,numpy; from rasterio.warp import transform"` 可自检。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m gridreproject demo`，`python -m gridreproject --help`，`python -m pytest -q`。运行与测试使用本地合成数据，不访问网络。

## 覆盖与运行结论

广延量的数值用面积汇总，覆盖率单独以有效像元比例计算；四格合一时即使三格的数值总和正确，缺失第四格也不能视为满覆盖。独立 `audit` 会复核源与输出哈希、网格和总量，发现问题写入 Issue 并将 Run 降为待核或失败。输出发布异常回滚 Run 与证据，并清除临时产物。

输出核验还逐格比对运行时持久化证据，并核对目标网格边界；即使输出哈希被同步改写，业务值或地理边界变化也会使 Run 失败。
