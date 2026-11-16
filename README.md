# 栅格重投台

离线单波段栅格重投影与覆盖审计。源登记、目标网格契约、逆映射、变量语义采样、缺测传播、覆盖诊断、输出再验和溯源报告构成完整流水线。运行期使用已安装 rasterio 1.5.2 与 numpy 2.5.3；`python -c "import rasterio,numpy; from rasterio.warp import transform"` 可自检。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m gridreproject demo`，`python -m gridreproject --help`，`python -m pytest -q`。运行与测试使用本地合成数据，不访问网络。
