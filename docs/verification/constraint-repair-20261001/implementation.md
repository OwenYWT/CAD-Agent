# Constraint repair implementation and verification

Baseline: main `8a90d445425e2c0c1ce9cbc35c2ccc97264d6185`.
Working branch: `fix/constraint-repair-evidence`.

## Authorized scope

1. Capture failed sketch geometry, constraints, driving/reference state, solver diagnostics,
   logical identities and execution progress before rollback. Bind evidence to the exact
   plan and checkpoint; diagnostic failure must not prevent rollback or create a candidate.
2. Backend-owned protection of persisted requirements, parameters and necessary geometric
   relationships; unknown provenance stays protected. Verify mathematical relationships,
   not just unchanged scalar values.
3. Scoped logical-constraint patches with baseline checks, deterministic identities,
   immutable committed operation records and reference/dependency checks before deletion.
4. Incremental solver diagnostics and complete profile checks before downstream features;
   no arbitrary dimensions or blanket geometry locking.
5. Persist rejected patches and precise differences, return actionable feedback, and bound
   repair by attempt/resource/progress policy. Preserve original and repair errors in UI.
6. Immutable engineering acceptance, real downstream recomputation, final artifact checks
   and isolated parameter perturbation checks before candidate publication.

## Verification sequence

- Failure snapshot: real kernel rollback, executed/failing/unexecuted mapping, evidence
  serialization failure, unchanged checkpoint/ledger; transport and persistence tests.
- Protection/patch: requirement lineage, derived/redundant relations, unknown references,
  stale baselines, unrelated mutations, deterministic replay and parameter dependencies.
- Solver: redundant plus underconstrained sketches, differing dimensions and operation
  orders, connected profiles and downstream feature recomputation in FreeCAD 1.1.3.
- Workflow/UI: repair rejection feedback, no-progress and resource exhaustion, original
  error retention, evidence access ownership, no candidate on any required-check failure.
- Full regression: architecture boundaries, backend/frontend, mandatory native contracts,
  workflow/replay and browser scenarios; live provider scenario only after deterministic
  chain verification. Record actual results and limitations here before completion.

## Evidence baseline

Incident `255d5e0b-c6de-467d-9b84-e34b43d346ce`: native solver reproduced the
redundant 35 mm total-height constraint; omitting that expression alone leaves three
degrees of freedom and fails at Pad. The previous repair output was not persisted, so
its exact out-of-scope differences are unknown. No production task has been retried.

## 本次实现

| 要求 | 实际接入 |
| --- | --- |
| 保存失败现场 | 在原生事务回滚前记录几何、约束、驱动/参考、求解状态、自由度、表达式和生成操作映射；绑定计划、检查点和失败操作。执行进度分为已执行、失败中、未执行。诊断提取或存储故障仍保留原始错误，继续回滚。失败快照单独存储，不能作为检查点或候选。 |
| 可信保护规则 | 后端读取冻结的验收要求和已保存构建输入，保护用户/确认尺寸及必要关系。精确有理数运算证明可推导关系；原生内核再次核验。来源不明、无法证明的关系及被表达式/别名引用的约束继续保护。模型没有声明“可修改”的接口。 |
| 局部补丁 | 工具只接受指定草图的逻辑约束新增、删除、替换。后端合成计划，保持其他草图、特征、导出和已提交操作记录不变。基线必须匹配；变更身份确定，同一输入得到同一计划。执行前重放补丁证明。 |
| 分阶段求解 | 保留逐操作求解，构建中允许欠约束；下游特征前检查完整约束和实际轮廓。闭合线框还须通过原生面域有效性检查，避免自交轮廓被拉伸成多个实体。不能随意加尺寸或固定全部几何。 |
| 修复反馈 | 越界、错误身份、无效关系等拒绝在工具循环内反馈并保存原补丁、差异和诊断读取记录。必须读完实际与待执行约束的分页。按尝试次数、工具调用和拒绝次数控制资源，按物理状态及执行进度识别重复修复。界面同时保留原始建模错误与修复失败原因。 |
| 独立验收 | 补丁不能改变冻结验收条件。检查真实草图关系、轮廓、下游实体；在隔离 FCStd 副本中扰动驱动尺寸和受影响的可编辑特征参数。按真实依赖图传播，复用属性编辑器的参数定义。表达式控制的值只随源参数联动，不能强行覆盖。所有必要检查及证据保存成功后，才允许发布候选。 |

关键实现位于 `backend/app/freecad/constraint_patch.py`、`constraint_relationships.py`、
`failure_snapshot.py`、`backend/sandbox/freecad_constraint_validation.py`，经现有生成器、
Model Job、Temporal、执行器、对象存储和候选流程接入。执行模块通过契约接口注入校验，
没有新增跨模块私有依赖例外。无数据库 schema 变更。

## 深层问题与修正

1. 原链路用回滚后的有效检查点诊断失败，缺少出错草图现场；又让模型重写整个计划，
   因而与保护其他几何的检查冲突。现在把失败诊断与有效检查点分离，只合成局部约束补丁。
2. “原数值仍在”不足以证明参数联动正确。对冗余总高的删除，后端证明其与原尺寸的关系，
   再在真实几何和参数扰动中复核；原点关系使用稳定 datum 引用，不额外暴露零长度驱动量。
3. 实际模型测试暴露了对未执行约束的重复添加。现在要求读完相关诊断分页，明确未执行操作
   仍在计划中；冗余判断只使用补丁保留的有效关系，不再把准备删除的约束算作替换后的依据。
4. FreeCAD 的 `TopologicalSortedObjects` 顺序不能直接当作下游执行顺序。现在从真实依赖图
   求受影响对象集合，原生圆角用例证明多级特征参数不再漏检。
5. FreeCAD 1.1.3 提供 `ExpressionEngine`，但本轮原生对象没有 `getExpression`。
   已兼容两种元数据读取方式，防止把表达式控制的参数误认为独立可编辑量。

## 原事故的结论

原任务除了约束冗余，还包含错误端点连接及自交的第二个草图。原生证据表明：
线框闭合、草图 Shape 有效、自由度为零，仍不足以证明能形成有效面域。
原轮廓在 `(0,-75)` 自交；直接拉伸会产生两个实体。

因此原轮廓作为**必须拒绝**的回归输入保留。局部约束补丁不获准偷偷改几何，
此情况需要重新规划几何；本次没有重试或改写历史生产任务。

成功链使用独立的有效 L 形测试输入，在修复开始前冻结该输入。25 mm 间隙和单一实体
使用独立测试验收条件检查。历史任务原有测量探针不正确，本次没有替换生产任务的验收条件。

## 已取得的验证证据

| 验证 | 结果与范围 |
| --- | --- |
| 后端完整离线回归 | 部署依赖环境：1,732 通过，209 跳过，1 项按标记排除。跳过的外部服务测试另行执行，不计为通过。 |
| 前端 | 159 通过，TypeScript 与生产构建通过。已有大 chunk 提示仍存在。 |
| 原生 FreeCAD | 22 份运行报告完成；包括快照/回滚、轮廓负例、隔离扰动、表达式联动，以及现有倒角、扫掠、旋转、阵列、曲面测量、FEA、CAM 和发布产物。 |
| 真实模型 | `kimi-k2.7-code` 固定有效基线：4 次修复、22 次参数扰动、候选提交通过；10 次真实调用，共 114,792 token，约 725 秒。初次运行未通过，其拒绝记录用于定位上述分页与校验问题。 |
| 浏览器 | 首候选、任务状态、参数拒绝/草稿、历史/布局/相机、监控权限、原始错误与修复失败并存均通过。新增失败用例使用真实工作流及内核，只控制 Provider 提案。 |
| 历史兼容 | 12 份固定旧发布历史回放通过；本次真实模型工作流 214 个事件只读回放通过，未重复调用模型。 |
| 架构与契约 | 架构边界检查、能力目录同步检查、差异空白检查通过。 |

| 最终专项 | 结果 |
| --- | --- |
| 完整持久工作流与 API 回归 | 55 通过、0 失败；8 个其他付费模型场景按 CI 规则排除。本次 Kimi 专项单独实测。涵盖真实 PostgreSQL、Temporal、对象存储、原生执行、取消、恢复、候选/版本、WebSocket、历史和文件归属。 |
| 数据库专项 | 事务/归属 80、Model Job 12、监控权限 6、旧 schema 升级 1；合计 99 通过，无跳过。 |
| 最终修复正反例 | 有效基线：诊断、拒绝反馈、修复、扰动、验收、候选、提交通过；原始自交基线：拒绝、无候选、旧版本不变。纳入上述 55 项必跑组合。 |
| 参数真实接口与独立产物 | 阵列孔数 6→7、旋转角度 90→120°、沉孔口径 8→9 mm；真实提交后再次读取 STEP，独立实测通过，保持一个实体、稳定特征 ID、其他参数和幂等/过期版本约束。 |
| 动态历史 | 6 份真实历史、4 个工作流家族只读回放通过，作为固定旧发布历史和本次 Kimi 历史回放的补充。 |

最终原生测试镜像内容身份为
`sha256:19f0d082b1a1a1fbb0c39e3ee07be7ddf03bb8ff9570d929d9fc1a530d978a47`。
真实模型运行后，最终镜像额外纳入 `ExpressionEngine` 读取兼容修正；
该修正已用真实参数/表达式内核用例、全部原生回归及完整持久链复核，没有再次消耗模型 token。
沙箱叠加的六份源码与工作树逐文件 SHA-256 一致。

本轮没有把付费模型场景的排除、Autodesk Fusion 实机或生产部署验证标成通过。
初次完整离线容器测试因临时镜像挂载缺少前端/部署文件而失败，补齐完整源码挂载后
重新运行全套，结果为上述 1,732 通过。没有为通过测试而删除断言或放宽验收。

### 复现入口

- 离线后端：`APP_ENVIRONMENT=test DURABLE_CONTROL_PLANE_ENABLED=false python -m pytest -m 'not docker and not llm and not fusion_e2e' -q`。
- 外部链：按 `.github/workflows/ci.yml` 的 durable MCAD integration gate 配置专用数据库、
  Temporal、S3 及沙箱后运行；本次新增用例为
  `tests/integration/test_temporal_mcadd_workflow.py::test_constraint_patch_incident_full_chain_and_owned_evidence`。
- 原生与浏览器：`scripts/ci/native_contracts.sh`、`scripts/ci/browser_contract.sh`。
  新增报告已登记到强制运行证据清单，缺报告或出现内核 traceback 会失败。
- 固定旧历史：`backend/tests/e2e/replay_released_histories.py`；动态历史：`replay_histories.py`。
- 前端：`node --experimental-strip-types --test tests/*.test.ts`、`npm run build`。

归档摘要见 [evidence-summary.json](evidence-summary.json)，包含实际报告摘要、哈希和源码身份。
完整本地运行日志保留在 `/private/tmp/cad-fixes-20260923/constraint-repair-20261001`；
其中运行环境与临时账号文件属于私有测试材料，未纳入仓库。

## 适用范围与未验证项

- 自动修改仅覆盖后端能够证明的关系；当前证明器覆盖轴向直线、圆及相关线性尺寸/连接/等长关系。
  未支持的曲线、必要非线性关系、外部引用或不明来源不会被放宽为可修改。
- 隔离扰动验证局部参数联动，不证明所有可能参数取值、实物配合、材料强度或制造条件。
  超出预算或无法验证时明确失败，不以“未发现错误”代替通过。
- 固定输入的修复成功不是端到端自由建模成功率评测。受控提案测试用于稳定复现错误和反馈链；
  真实模型测试单独记录。此案例没有完成实物制造/适配验证，DFM 在该链路测试中显式关闭。
- 本轮不增加模型输出 token 上限或生成等待超时。资源策略可配置：修复尝试默认 6 次、
  每轮工具调用 8 次、拒绝 3 次、补丁 32 项、原生扰动 128 次；这些是明确的资源预算。
- 所有工作在 `CAD-Agent-native-reliability` 的修复分支；未推送 GitHub、未部署腾讯云，原项目目录未修改。
