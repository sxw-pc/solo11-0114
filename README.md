# 栅格重投台

离线单波段栅格重投影与覆盖审计。源登记、目标网格契约、逆映射、变量语义采样、缺测传播、覆盖诊断、输出再验和溯源报告构成完整流水线。运行期使用已安装 rasterio 1.5.2 与 numpy 2.5.3；`python -c "import rasterio,numpy; from rasterio.warp import transform"` 可自检。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m gridreproject demo`，`python -m gridreproject --help`，`python -m pytest -q`。运行与测试使用本地合成数据，不访问网络。

## 覆盖与运行结论

广延量的数值用面积汇总，覆盖率单独以有效像元比例计算；四格合一时即使三格的数值总和正确，缺失第四格也不能视为满覆盖。独立 `audit` 会复核源与输出哈希、网格和总量，发现问题写入 Issue 并将 Run 降为待核或失败。输出发布异常回滚 Run 与证据，并清除临时产物。

输出核验还逐格比对运行时持久化证据，并核对目标网格边界；即使输出哈希被同步改写，业务值或地理边界变化也会使 Run 失败。

## 分区风险与产品发布

`zone-register` 冻结目标格分区与阈值，`zone-evaluate` 根据持久化格值及覆盖证据裁定正常、超限或证据不足。`release` 将分区结论和运行审计合并成准予发布、拒发或暂缓的业务决定。相同栅格可因分区约束不同形成不同发布结果；NoData 或输出篡改不会被放行。

`trend-register` 固化两次运行、分区及变化预算，`trend-evaluate` 双边审计后逐格存储变化证据。均值未变但局部热点超限仍判恶化；改善、稳定、覆盖缺口和输出篡改各有不同结论。

`archive-request` 冻结一组运行及 product_only/full_replay 用途。旧 `archive-export` 保留原逐运行复制语义与已确认运行、发布记录不变；新 `archive-publish` 以内容哈希为对象身份生成去重、原子封存的离线包，`archive-verify` 离线核验，`archive-restore` 在全新目录与 SQLite 库中恢复。

## 内容寻址封存与诚实恢复

`publish(request_id, archive_root, byte_budget)` 在私有临时区组装封存包：所选 run 的输出/源 TIFF、合同正文、源元数据以及 run 与全部 cell/issue 证据构成闭合清单，多个 run 共用同一源哈希时源字节只存一份、引用各自独立。复制期间逐对象重新计算哈希，audit 后被替换的源路径会以 `object_changed_after_audit` 拒绝；数据库读取在 `BEGIN IMMEDIATE` 冻结视图内完成，旧 cell 行与新输出文件无法拼成伪完整包。预算只统计实际新写入的唯一对象字节与清单开销，重复来源不按 run 重复计费；预算不足只给 `byte_budget_exceeded` 诊断，绝不发布缺对象的包。所有对象、引用、清单与离线验证通过后才一次性 `os.replace` 公布，同请求同预算重试幂等且不覆盖已签包，失败清理临时物。

两种用途的恢复保证不同且明确标注：`product_only` 包标为 `display_only`，原文件消失后仍可恢复可打开、网格/边界/哈希符合清单的输出 TIFF，但不声称重新验真源与 cell 语义；`full_replay` 包独立于原数据库与原路径，在全新目录和 SQLite 库中重建 source/contract/run/cell/output 引用链，再调用现有 `audit` 达到与归档时相同的已核验状态。恢复产生新路径与 ID 映射，绝不更新原库 `source.path` 或覆盖现有输出。清单、共享源对象、单格 cell 或恢复后 GeoTIFF 被删改时，离线 `verify_bundle`/`restore` 逐对象重算哈希并拒绝对应保证，不接受 manifest 自报的总摘要。
