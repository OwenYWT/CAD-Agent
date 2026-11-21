# A / C 首版开发交接（2026-11-13）

工作区：`/Users/wentao/CAD-Agent-native-reliability`；分支：`compare_YWT`；开发基线：`a4e640af2f2a1bca758225d1aeb39402ca245a89`。本文是开发自测记录，不是独立验收结论。11 月 13 日开发交接时改动尚未提交、推送或部署；后续发布以对应部署回执为准。

按用户本次选择，只实施 A（原生拓扑引用）和 C（独立几何测试、冻结重放与同故障缩减）。没有开发 B 的阶段目标、阶段回执或新增检查点放行条件，也没有恢复自然语言输入扫描。

## 问题与处理

| 范围 | 原有问题 | 本次处理 |
|---|---|---|
| A：引用身份 | v1 先按几何重新寻找边，再取得当前原生名字；坐标、半径改变后，几何相似不等于仍是原来的边。 | 保留 v1，新增 v2 不透明引用。以受控创建操作、对象出生身份、完整 FreeCAD ElementMap 和保存文件身份关联引用。重新使用时核对同一来源、修订、对象、唯一原生映射及几何类型，不按距离挑选替代边。 |
| A：持久化与信任 | FCStd 自带属性不能证明它由服务端受控操作产生；只保留名称可能接受删除后重建的对象。 | 元数据放在与 FCStd SHA-256 绑定的 state 中；执行端记录，接收端再核对实际计划、继承对象和依赖目录。导入文件不获得受控创建身份，任意 `api.execute` 会清除这类身份。 |
| A：已有下游引用 | 原边还存在，并不证明原倒角或圆角仍引用它。 | 在受控操作后检查已登记的下游特征实际 Base 引用；改绑到其他孔会失败。 |
| C：测试组织 | 已有很多实际内核测试，但缺少统一的独立公式、冻结输入、跨案例报告和同故障缩减链。 | 复用现有类型化操作与 FreeCAD 沙箱，增加无模型调用的解析配方、固定种子、文件回读、精确操作重放和有限预算缩减。 |

所有几何成功或失败均来自实际 FreeCAD 执行及文件测量；业务路径没有固定成功返回值。测试中的固定数值是测试输入和独立数学期望，不参与用户请求处理。

## A 的实现范围

- 新增 `backend/app/contracts/native_references.py`：共享数据契约、文件绑定、创建目录及依赖过渡核验。
- 新增 `backend/app/freecad/native_references.py`：使用完整原生映射解析边、面，记录覆盖数量；缺映射或多来源别名不退回 `EdgeN`、最近坐标。
- `backend/sandbox/freecad_entry.py`、`state_projector.py`：接入现有事务、保存重开和导出链；任意 Python 路径清除未经证明的身份。
- `backend/app/workflows/handlers/cad_execution.py`：从已经校验的修订或检查点加载目录，上传结果前核对输出目录与实际计划；目录错误保留明确错误码。
- `topology/contracts.py`、`freecad/contracts.py`、`domain/selection.py`、`freecad/selection.py`：圆角和倒角接受 v2；旧 v1 输入（包括省略版本号的输入）仍可使用；冻结的用户选择不因增加格式而扩大权限。
- `agent_tools.py`、`operation_generator.py`：工具说明同步公开 `references` 查询和返回的 v2 选择器用法；用户已经冻结子元素时仍使用其原选择。
- `inspection.py`、`semantic_state.py`、`state_contract.py`：只读发布原生引用、创建来源和覆盖信息；不改变旧的特征内容指纹。检查接口沿用每页最多 32 条及原有响应预算。
- `backend/app/api/documents.py`：公开查询使用已经验证的修订号发布引用；不把修订号写回历史 state，不改变原检查结果的状态哈希。
- `cloud_documents.py`、`durable_submission.py`：提交选择时读取完整且校验过的 state 目录，不用界面前 32 项摘要决定修改授权。六孔模型第二页引用的修复前误拒绝与修复后完整提交证据均已保留。
- 前端内核细节增加“原生引用”读取项；未重写画布拾取，现有画布选择仍保留原逻辑。Agent 和 API 可使用已发布的 v2 引用。
- 两份 sandbox Dockerfile 与腾讯云镜像文件核验脚本同步打包共享模块，没有添加新部署服务或数据库表。

首版运行证据覆盖同一文档内矩形 Pad、双圆孔 Hole/Pocket、选择一个孔口的 Chamfer/Fillet，以及保存、重开、改孔径、移孔和继续重算。完整目录不依赖旧的前 64 项拓扑摘要；界面摘要最多发布 32 个引用，更多引用通过检查接口分页读取。引用里的坐标来自所属对象的 Shape，不是跨实例坐标承诺。

v2 的写入消费范围是圆角/倒角；工程测量接口和画布拾取没有切换到 v2。

**能力边界：**这不是通用拓扑命名问题的完整解决，也不是 SolidCraft 全量逐面 History 的移植。没有实现任意面分裂/合并的语义恢复、跨文档传播、App::Link 实例身份或任意 Python 重建后的身份恢复。没有证据时，不发布新的可信写入引用；不把 FCStd 自定义属性、几何近似或普通序号称为来源证明。旧 v1 仍是几何选择，不追授跨修改身份。

## C 的实现范围

- `backend/benchmark/native_oracle.py`：配方校验、独立解析公式、现有操作编译、固定输入、重放、报告及缩减。
- `backend/benchmark/native_oracle_worker.py`：实际 FreeCAD 执行，独立重开 FCStd/STEP，严格形状检查和布尔差集比较。
- 首版解析域是长方体板及完整位于板内、互不重叠的圆柱盲孔/通孔。独立计算体积、面积、质心、包围盒、孔位、孔径、深度和贯穿状态。每种物理量有自己的数值比较尺度；不修改产品工程公差。
- 固定 7 例包括无孔、偏心贯穿、盲孔、平移、旋转、缩放和双孔；再使用 32 个固定种子改变合法尺寸、孔位、孔径、深度和姿态。没有把不支持的复杂形状偷偷计为成功。
- `case.json`、`operations.json`、`expected.json`、`input.json` 在运行前落盘。哈希对应规范化 JSON 内容；重放使用冻结操作，不重新生成计划。原始文件保留。
- 每例独立禁网、只读容器，限制 CPU、内存、进程数和时间；超时清理对应容器。报告核对输入身份、调用 nonce、文件 SHA-256 和运行时代码。运行及缩减锁定实际镜像 ID。
- 缩减先核对原失败报告的冻结输入和镜像，再要求相同错误类别、错误码、诊断、失败操作/断言及运行镜像；原失败先重放两次，缩减后再确认两次。不能把当前重放出现的另一种错误改称原故障。最多 16 次重放、180 秒、256 MiB 工件预算；预算耗尽会标记未确认，不宣称全局最小。
- 无法确定原故障的崩溃或超时不做“同故障缩减”。没有接入第二套几何内核，也没有用同一 OCCT 的另一个封装冒充独立真值。

`backend/tests/test_native_references.py`、`test_native_oracle.py` 覆盖协议及公式；两份新增 e2e 脚本运行真实几何正反例；真实 Temporal 集成测试覆盖检查点、候选、审核提交、刷新重载和带选择的修改。新增测试及报告已接入现有 `scripts/ci/native_contracts.sh`、架构边界及 CI 必需证据清单。

## 验证状态与证据

证据根目录：`/private/tmp/cad-ac-adaptation-20261112`。这是本轮目录的原始创建日期，运行跨到 11 月 13 日后未重命名或覆盖之前的失败证据。

测试镜像：`localhost/cad-native-ac:20261112`；实际 ID：`892fa85be02e12ab4a81f7d5ba28a2535a5e1436de1e4158c6e432594c5f6808`。`image-source-verification.json` 已核对 Dockerfile 声明的 44 个运行文件，源码 SHA-256 全部相同。

代码冻结清单见 `final-code-source-manifest.json`，记录 33 个新增/修改代码及测试配置文件的 SHA-256；文档不计入代码清单。审阅副本包含 `final-tracked.patch` 和 `final-source-overlay/`（8 个新增源码/测试文件）。原有未跟踪文件清单保存在 `initial-untracked.z`，`workspace-preservation.json` 核对 74,443 个原有路径仍存在，暂存区为空；这不是原有全部文件内容逐字节不变的声明。

| 验证 | 结果 / 证据（相对证据根目录） |
|---|---|
| 后端完整快速套件 | 最终冻结版本 2,804 通过、418 跳过、1 取消选择：`quick-backend-frozen/`；另有 Fusion 子集 189 通过（与后端重叠），CI 契约 61 通过、40 个子测试通过。跳过项不计为已执行，本轮另行运行下述服务/原生套件，未声称所有跳过项都得到补跑。 |
| 前端 | 179 通过；lint、TypeScript 和生产构建通过：`quick-frontend-final/`。本轮未新增浏览器自动化验收。 |
| 架构与 CI 策略 | 模块边界、功能地图和 CI 必需证据检查通过：`architecture-complete-catalog/`。 |
| A 原生链 | 保存重开、改孔径、移孔、超过 64 条边、伪造来源、错误来源/对象/修订、对象消失/重建、依赖改绑、Hole/Pocket 与 Chamfer/Fillet 组合：`reference-variants.log`、`reference-variants/`。 |
| A 完整目录回归 | 第二页合法引用修复前确实被阻断：`public-reference-overflow-before.log`；修复后真实生成、检查点、审核提交、第二页查询、选中修改、再次提交及 FCStd/STEP 回读通过，1 项 / 42.45 秒：`public-reference-overflow-after.log`、对应 XML 和工件。另有 18 项相关单元测试通过：`full-catalog-selection-unit.log`。 |
| C 解析与种子 | 39/39 通过，运行镜像固定：`mcad-core/native/native-oracle/report.json`；固定操作重放 1 项通过：`oracle-replay-final/report.json`。 |
| C 反例与缩减 | 3 个有效但错误的模型均被解析验收拒绝；同故障 10→7 步并重复确认；错误原故障、错误镜像均拒绝：`mcad-core/native/native-oracle-controls/report.json`。独立专项亦通过：`oracle-controls-recorded-failure/report.json`；对应公式单元测试 9 项通过：`oracle-recorded-failure-unit.log`。 |
| 完整真实服务 | 210 通过、零跳过、9 取消选择，耗时 3,488.63 秒：`mcad-core/durable-core.xml`。启动版本与最终小范围修复的区别见下段。 |
| 原生运行时 | 16 通过、零跳过：`mcad-core/native-runtime.xml`。 |
| 工作流回放 | 6 条当前历史和 12 份发布历史通过：`mcad-core/workflow-replay.json`、`released-workflow-replay.json`。 |
| 完整原生契约 | 45 个步骤通过（包含本轮 A / C，也包含既有材料、装配、测量等测试）：`mcad-core/native/steps.json`。核心套件退出码 0；全部 48 份必需运行证据通过现有校验器：`core-evidence-verification.log`。 |

以上范围重叠，不合并计数。完整服务套件在公共查询和完整目录授权修复前启动，其 Python 进程加载的是启动时版本；这两项最终改动由独立数据库中的 `public-reference-overflow-after` 全链路回归及最终单元套件覆盖。后续原生契约子进程使用最终文件与同一锁定镜像。不能把两轮结果描述为同一冻结工作区下的一次全量运行。

中间失败日志与模型已保留，包括测试工具早期路径、读回聚合质心、测试协议接入错误，以及完整目录授权的修复前证据；没有把这些失败改记为通过。

本轮付费模型调用为 0。真实服务测试使用固定模型输入经过现有适配器，FreeCAD、PostgreSQL、Temporal、对象存储与审核/提交是真实运行。不能据此声称真实 AI 成功率、视觉验收、DFM 或实物性能得到提升/验证。

## 2026-11-14 发布前补充

独立验收确认 A/C 核心链路通过，发现 UI01/P3：新增“原生引用”标签缺少英文翻译。发布前仅在现有 `frontend/src/i18n/I18nProvider.tsx` 词典补充 `Native references`，没有修改引用和执行协议。沿用验收团队的真实组件浏览器脚本，在独立目录复验：中文显示“原生引用”，英文显示“Native references”，页面错误为 0；前端 179 项、lint、类型检查和构建通过。原验收失败证据未覆盖。

该补充及发布证据保存在 `/private/tmp/cad-ac-deploy-20261114`。提交时另清理共享目录合同文件末尾的空行，AST 未变。

腾讯云 Linux 复验补充：Oracle 的直接容器入口未沿用现有沙箱的挂载权限处理。宿主创建的输出目录对 UID 1000 的内核不可写；严格 umask 下输入也不可读。已按 `sandbox/executor.py` 的既有方式使新建输入可读、独立输出可写，输入挂载仍只读，父目录权限保持原样；没有修改公式、几何验收或内核身份。此项发生在最初冻结记录之后，部署证据单独保留修复前后结果，不把先前 macOS 测试结果视为 Linux 验证。

2026-11-15 的 GitHub CI `43607809004` 在真实服务层得到 209 通过、1 失败、9 取消选择，未执行后续原生契约；失败发生在新增引用集成测试的最终文件回读。该测试把 pytest 默认 `0700` 的临时目录直接挂载给 UID 1000 的 FreeCAD，导致文件不可读。使用该轮 CI 保留的同一份 FCStd/STEP 和完全相同的几何断言，在腾讯云复现 `0700` 失败、仅目录改为 `0755` 后通过，模型字节未修改。集成测试按同文件已有测试的方式补充目录权限，未修改任何业务实现、测试选择或几何断言。修复后在独立本地测试数据库复跑同一完整工作流，1 项通过 / 43.20 秒。原失败 ZIP、哈希和前后日志保存在发布证据目录；最终完整 CI 结果以发布回执为准。

## 复跑入口

在项目根目录设置现有 `SANDBOX_COMMAND`、`SANDBOX_IMAGE` 和 Python 路径；服务套件另需独立测试服务配置，不要输出凭据，不要并发清理同一数据库。

```bash
export PYTHONPATH="$PWD:$PWD/backend"
export SANDBOX_COMMAND=podman
export SANDBOX_IMAGE=localhost/cad-native-ac:20261112
export CAD_CI_PYTHON=/private/tmp/cad-source-intake-20261028/venv/bin/python

# 独立解析测试；每次使用全新证据目录。
"$CAD_CI_PYTHON" -m benchmark.native_oracle run --output /private/tmp/cad-oracle-new-run --seeds 32
"$CAD_CI_PYTHON" backend/tests/e2e/native_oracle_controls.py /private/tmp/cad-oracle-new-controls

# 固定操作重放；不重新调用编译器或模型。
"$CAD_CI_PYTHON" -m benchmark.native_oracle replay \
  --source /private/tmp/cad-oracle-new-run/offset-through \
  --output /private/tmp/cad-oracle-new-replay

# 确认同类故障后缩减；原始失败文件不修改。
"$CAD_CI_PYTHON" -m benchmark.native_oracle minimize \
  --source /private/tmp/cad-oracle-new-controls/wrong-direction \
  --output /private/tmp/cad-oracle-new-reduction

# 按现有 CI 入口运行单元、类型、架构、真实服务及原生契约。
bash scripts/ci/run.sh quick backend
bash scripts/ci/run.sh quick frontend
bash scripts/ci/run.sh quick architecture
# 加载独立测试配置后，必须设置 SANDBOX_RUNTIME=podman：
bash scripts/ci/run.sh core
```
