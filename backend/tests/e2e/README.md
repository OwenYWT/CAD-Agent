# 真实 Cloud CAD 端到端验收

这些脚本访问真实 HTTP/WebSocket、PostgreSQL、MinIO、Temporal、模型 provider、FreeCAD、Gmsh/CalculiX 和本地文件系统。浏览器使用 Python Playwright/Chromium；恢复负例只丢弃或延迟真实响应，不替换业务响应。新版方案结果见 [2026-09-13 实施报告](../../../docs/qa/native-coediting-2026-09-13.md)，此前问题见 [独立验收整改报告](../../../docs/qa/acceptance-remediation-2026-09-10.md)；[历史融合报告](../../../docs/qa/cloud-cad-fusion-2026-09-09.md) 不代表当前完整方案通过。

## 前置条件

从仓库根目录执行。启动隔离 API、Worker、前端和基础设施，迁移到 `0027_document_scene_tasks`，配置真实模型凭据与 bootstrap admin。普通集成 pytest 使用的数据库 fixture 会清理测试数据，必须与本组 E2E、开发用户及生产数据库分开。内核在共享小内存机器上串行运行。

Python 需要项目依赖、`pytest`、`httpx`、`python-dotenv`、`websockets` 和 Playwright 的 Chromium。原生执行使用完整打包沙箱；API/Worker 的绝对临时目录必须能被兄弟内核容器读取。

以下是本轮隔离环境的 macOS/Podman 配置示例。仅对你为验收启动的容器设置停启变量；复跑主流程应设置全新的账号及报告路径，不能覆盖旧证据：

```bash
export CAD_NATIVE_E2E_URL=http://127.0.0.1:8040
export CAD_NATIVE_E2E_WEB=http://127.0.0.1:8100/
export CAD_NATIVE_E2E_ENV=/private/tmp/cad-coedit-20260912/live/container.env
export CAD_NATIVE_E2E_PRIVATE=/private/tmp/cad-cloud-new-run-private.json
export CAD_NATIVE_E2E_REPORT=/private/tmp/cad-cloud-new-run-http.json
export CAD_NATIVE_BROWSER_REPORT=/private/tmp/cad-cloud-new-run-browser.json
export CAD_NATIVE_BROWSER_SHOTS=/private/tmp/cad-cloud-new-run-shots
export CAD_NATIVE_E2E_API=cad-coedit-20260913-api
export CAD_NATIVE_E2E_WORKER=cad-coedit-20260913-worker
export CAD_NATIVE_E2E_FRONTEND=cad-coedit-20260913-frontend
export CAD_NATIVE_E2E_POSTGRES=cad-coedit-20260913-postgres
export CAD_NATIVE_E2E_REQUIRE_ADDRESS_CHANGE=1
```

`CAD_NATIVE_E2E_ENV` 读取该隔离部署的 `ADMIN_PASSWORD`，不输出配置内容。主流程会创建随机真实测试账号并将凭据写入 `0600` 私有文件。每轮使用新的私有/报告路径，保留需要追溯的旧证据。本轮复跑的扩展脚本用 `CAD_NATIVE_E2E_EVIDENCE_DIR` 指定 fixture/报告根目录，未设置时兼容 `/tmp/cad-expansion-*`。尚未迁移的旧脚本仍使用 `/tmp`，不能混用旧证据或并行共享同一套 fixture。

## 新版共同编辑与故障验收

先完成真实主文档生成、浏览器手改及上述私有账号准备。下列脚本每轮写入新的 `CAD_COEDIT_REPORT_DIR`；需要 DB 断言的脚本还要求 `CAD_NATIVE_E2E_HOST_ENV` 指向同一个隔离环境的 JSON 配置。停启类脚本仅接受本机 `cad-coedit-*` 资源。

| 脚本 | 前提与覆盖 |
| --- | --- |
| `native_coediting_browser.py` | 主文档已有 Hole=9；读取当前 Pad 厚度并要求 AI 增加 2 mm。验证冻结选择、草稿、同模型续改和提交 |
| `native_packaged_browser.py` | 正式镜像独立环境、owner 私有会话及真实两 Body fixture；浏览器参数修改、刷新恢复、审查提交、原生状态/文件哈希与 3D 一致性 |
| `native_first_candidate_browser.py` | 真实确定性编译器/Temporal/FreeCAD 生成首个未提交候选；候选只读、空 Head 新建意图、浏览器应用后可编辑。仅扣留一条外发提示检查意图，不替换业务响应或调用假 Provider |
| `native_recovery_browser.py` | 至少一个参数不同的同面板历史版本；验证真实 WS ACK 丢失、刷新查询、接受后 commit 传输失败、同候选重试、回退 ABA |
| `native_view_identity_browser.py` | 两个已提交原生历史版本；延迟真实历史响应，验证后选版本、只读参数、3D、导出和非当前回退拒绝 |
| `native_scene_jobs.py` | 未缓存的原生修订；双用户并发去重、三档 LOD 哈希、命中计时、Head 不变 |
| `native_scene_controls.py` | 两个未缓存原生历史版本和显式 Worker 名；排队取消、撤权、失败 GET 不重启、并发显式 retry |
| `native_multi_hole_selection.py` | 用 seed 脚本 `--four-holes` 创建真实一 Hole 四轮廓 fixture，路径由 `CAD_MULTI_HOLE_FIXTURE` 指定；六种非法指代不入队，明确整体修改后独立量测 |
| `native_revocation_controls.py` | 当前协作 fixture 和真实 editor 账号；候选原先可下载，撤权后下载/审查/提交被拒，排队建模 0 次执行 |
| `native_temporal_replay.py` | 在配置真实 Temporal 的容器中运行，设置 `PYTHONPATH=/app/backend`；`CAD_TEMPORAL_REPLAY_INPUTS` 指定历史/任务，输出目录使用 `CAD_TEMPORAL_REPLAY_REPORT_DIR` |
| `native_joint_restore.py` | `CAD_JOINT_RESTORE_DIR` 必须不存在；显式 API/Worker 名、测试镜像、DB/对象配置。暂停本轮服务并最终恢复，备份后在新 DB/bucket 启动只读核验 API，验证认证、历史文件和全部行/对象哈希 |

联合恢复仅接受 `cad_coedit_live_*` / `cad-coedit-live-*` 源，目标必须全新；不删除已有资源。它覆盖实际启用的 PostgreSQL 认证、MCAD 数据及包含不可变历史文件的 S3 当前对象版本，不声称恢复 Temporal 整库、所有 S3 VersionId 或可选桌面 Connector 状态。密码、原始历史、trace 和备份必须留在私有证据目录。

视觉预算控制负例在 `tests/integration/test_temporal_mcadd_workflow.py -k native_visual_negative_budget_with_real_kernel`：使用明确标识的受控 Provider 判断，实际 PostgreSQL/Temporal/FreeCAD/渲染/S3 均执行。三个场景是 advisory 明确失败、required 明确失败、required 无法判断。此类结果证明门禁与预算控制，真实 Provider 的尺寸、孔位和修复效果须另测，不能互相替代。

## 按依赖执行

先建立真实模型并完成浏览器参数编辑：

```bash
python backend/tests/e2e/cloud_document_acceptance.py
python backend/tests/e2e/cloud_document_browser.py
```

用真实原生操作创建协作、实例和草图模型；以下入口通过 API 容器中的实际应用、Temporal、沙箱、对象存储和审查提交创建 fixture，不直接插入完成结果：

```python
import json, os, subprocess
from pathlib import Path

private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
script = Path('backend/tests/e2e/seed_collaboration_document.py').read_text()
for kind, flags in {
    'collaboration': [], 'scene': ['--instance'],
    'large-scene': ['--many-instances'], 'sketch': [], 'triangle': ['--triangle'],
}.items():
    result = subprocess.run([
        'podman', 'exec', '-i', '-e', 'PYTHONPATH=/app/backend',
        os.environ['CAD_NATIVE_E2E_API'], 'python', '-',
        private['owner']['user']['id'], private['project_id'], *flags,
    ], input=script, text=True, capture_output=True, check=True, timeout=240)
    record = next(line.split('=', 1)[1] for line in result.stdout.splitlines()
                  if line.startswith('CAD_COLLABORATION_DOCUMENT='))
    Path('/tmp/cad-expansion-'+kind+'-document.json').write_text(record)
```

保留认证 subject 的原始字符串；不要把无连字符 UUID 改写后再传给 `user_principal`。

继续依次执行以下脚本；每个命令失败时先修复并重新验证该阶段，不能继续把旧 JSON 当作当前结果：

```bash
set -e
python backend/tests/e2e/cloud_collaboration_acceptance.py
python backend/tests/e2e/cloud_collaboration_browser.py
python backend/tests/e2e/cloud_lease_expiration.py
python backend/tests/e2e/cloud_scene_browser.py
python backend/tests/e2e/cloud_large_scene_browser.py
python backend/tests/e2e/cloud_sketch_browser.py
python backend/tests/e2e/cloud_branch_acceptance.py
python backend/tests/e2e/cloud_merge_acceptance.py
python backend/tests/e2e/cloud_branch_browser.py
python backend/tests/e2e/cloud_engineering_acceptance.py
python backend/tests/e2e/cloud_engineering_browser.py
python backend/tests/e2e/cloud_cam_browser.py
python backend/tests/e2e/cloud_engineering_controls.py
python backend/tests/e2e/cloud_agent_inspection_acceptance.py
python backend/tests/e2e/cloud_engineering_agent.py
python backend/tests/e2e/cloud_release_acceptance.py
python backend/tests/e2e/cloud_release_browser.py
python backend/tests/e2e/cloud_bridge_acceptance.py
python backend/tests/e2e/cloud_bridge_browser.py
python backend/tests/e2e/cloud_bridge_failure.py
```

租约测试真实等待约 90 秒；Bridge 测试真实等待至少 305 秒。设置 `CAD_NATIVE_E2E_API` 时，Bridge 同时执行 `cloud_proxy_restart.py`：重启 API，保持前端运行，检查实际地址变化、HTTP 查询参数与已打开页面/会话 WebSocket 恢复。Docker 的普通 restart 可能保留 IP；需要覆盖地址变更时，应使用确实会更换地址的隔离拓扑，不能把未改变地址写为已验证。

数据库验收脚本为 `cloud_branch_database.py`、`cloud_engineering_database.py`、`cloud_engineering_agent_database.py`、`cloud_release_database.py`。在同一验收 API 环境以 `PYTHONPATH=/app/backend` 执行，参数分别为原始 owner subject 加 branch document ID、engineering workflow ID、Agent workflow/analysis workflow ID、release workflow/delivery ID。ID 从当轮 JSON 中读取；这些检查读取实际记录，禁止修改的语句和用于验证原子性的队列写入均明确回滚。

独立原生数值与几何入口为 `freecad_fea_acceptance.py`、`freecad_cam_acceptance.py`、`freecad_release_acceptance.py`、`dfm_geometry_acceptance.py` 等，需要在包含实际 FreeCAD/Gmsh/CalculiX 的沙箱环境执行。已有 pytest 原生门槛使用 `RUN_REAL_PODMAN=1`、`RUN_REAL_FREECAD=1`、`RUN_REAL_FREECAD_AGENT=1`；未提供的 provider/设备条件会跳过，不能算作实机通过。

## 独立验收反例与修复复验

复用当轮私有账号，但给每个测试设置新的报告目录，保留原始失败记录：

```bash
CAD_HOLE_CASE=four_corners CAD_HOLE_REPORT_DIR=/private/tmp/cad-holes-four python backend/tests/e2e/cloud_hole_semantics_browser.py
CAD_HOLE_CASE=blind_offset CAD_HOLE_REPORT_DIR=/private/tmp/cad-holes-blind python backend/tests/e2e/cloud_hole_semantics_browser.py
CAD_HOLE_CASE=different_diameters CAD_HOLE_REPORT_DIR=/private/tmp/cad-holes-two python backend/tests/e2e/cloud_hole_semantics_browser.py
CAD_REPAIR_REPORT_DIR=/private/tmp/cad-dfm-repair python backend/tests/e2e/cloud_dfm_repair_browser.py
CAD_REPAIR_REPORT_DIR=/private/tmp/cad-dfm-repair python backend/tests/e2e/cloud_repaired_candidate_commit.py
CAD_DFM_BUDGET_REPORT_DIR=/private/tmp/cad-dfm-budget python backend/tests/e2e/cloud_dfm_budget_browser.py
CAD_REPAIR_FAILURE_REPORT_DIR=/private/tmp/cad-impossible-fillet python backend/tests/e2e/cloud_repair_failure_browser.py
CAD_EXACT_PARAMETER_SOURCE=/tmp/cad-expansion-agent-inspection-http.json CAD_EXACT_PARAMETER_REPORT_DIR=/private/tmp/cad-exact-parameter python backend/tests/e2e/cloud_exact_parameter_failure.py
CAD_VISUAL_REPAIR_REPORT_DIR=/private/tmp/cad-visual-repair python backend/tests/e2e/cloud_visual_repair_browser.py
CAD_VISUAL_STAGE_REPORT_DIR=/private/tmp/cad-visual-repair python backend/tests/e2e/cloud_visual_staging_evidence.py
CAD_VISUAL_BUDGET_REPORT_DIR=/private/tmp/cad-visual-budget python backend/tests/e2e/cloud_visual_budget_browser.py
CAD_VISUAL_STAGE_REPORT_DIR=/private/tmp/cad-visual-budget python backend/tests/e2e/cloud_visual_staging_evidence.py
CAD_FAILED_TASK_REPORT_DIR=/private/tmp/cad-visual-budget CAD_EXPECTED_FAILURE_CODE=agent_visual_validation_failed python backend/tests/e2e/cloud_failed_task_browser.py
```

孔模型与 DFM 修复不能只看任务成功：以 `freecad_hole_measurements.py` 在真实 FreeCAD 沙箱分别重开 FCStd、STEP，检查孔数量、位置、直径、深度、底厚和体积。测量输入是实际下载路径及明确预期尺寸组成的 JSON，通过 `CAD_HOLE_MEASUREMENTS` 传入。FreeCADCmd 可能在 Python 脚本断言失败后仍返回 0，需同时核对成功标志；这些新测量脚本会在异常时显式非零退出。

视觉专项的 `visual-prototype.json` 是作为真实浏览器用户输入提交的单孔操作清单，不是替换 provider 的返回值。正例要求最终四角四孔；负例冻结初始操作，只允许追加四角孔，最终标准仍禁止中心孔。只有实际产生两轮视觉判断、真实修复与相应结果才计覆盖；模型直接做对、拒绝操作生成或不遵守测试限定都不计预算耗尽通过。下载脚本读取真实暂存记录，封装后按同任务的哈希/类型/长度读取已提升工件；不重新生成模型。对正例独立测量完成后，运行 `CAD_REPAIR_REPORT_DIR=/private/tmp/cad-visual-repair python backend/tests/e2e/cloud_repaired_candidate_commit.py` 核对实际审查提交。

2026-09-10 的真实视觉预算负例未通过；本轮受控视觉判断加真实内核的三个预算用例已通过，不能替代真实 Provider 语义验收。2026-09-13 的真实 DFM 修复测试遇到 `ProviderQuotaError`，额度恢复前记为 blocked。`cloud_failed_task_browser.py` 必须给出精确预期错误码；模型服务超时或额度不足后的界面/版本隔离不能替代 `agent_visual_validation_failed` 的预算负例。逐次结果以最新报告为准。

`cloud_semantic_database.py` 在 API 容器接收原始 owner subject 和当轮工程 Agent workflow ID，核对 Temporal 中实际发送给 provider 的上下文哈希、原生状态工件哈希、同修订 DFM 证据及特征修订历史。未知用途不做推断，缺少对应证据的检查不写成通过。

`freecad_semantic_binding_acceptance.py` 在真实 FreeCAD 中逐一解析导出 checkpoint 的物理拓扑绑定，并验证旧修订必然被拒绝；输入环境变量为 `CAD_SEMANTIC_BINDINGS`，JSON 包含 `snapshot` 和实际 `fcstd` 路径。`cloud_projection_upgrade_browser.py` 用 `CAD_PROJECTION_LEGACY_INPUT` 读取升级前版本 2 的同一 JSON，用 `CAD_PROJECTION_REPORT_DIR` 写浏览器证据；要求模型 head、字节与特征历史均未改变，只移除错误的基准对象绑定。
