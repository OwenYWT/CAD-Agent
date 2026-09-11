# 云文档 P0 融合验收

日期：2026-09-08。依据 `/Users/wentao/Downloads/AI_Native_CAD.html` 的分期目标及当前代码实施。验收范围是可运行的云文档 P0 与基础多人审阅；P1/P2 的完整工程平台不在已完成范围内。

## 已接入的能力

| 能力 | 实际实现和验收边界 |
| --- | --- |
| 云文档 | 复用 branch/revision/Change Set；已提交 head 是事实来源，候选不会自动覆盖当前模型。 |
| 文档串行修改 | PostgreSQL 操作队列贯穿 Temporal 规划、确认、执行和验证；revision/state version 双重检查、幂等提交、明确拒绝过期请求。 |
| FreeCAD 连续编辑 | 打开真实 FCStd、allowlist 操作、事务回滚、重算、重新保存；参数操作直接编译，连续修改及改回原值可重试。 |
| Agent 语义上下文 | 实际对象类型、依赖、参数和形体状态，稳定特征 ID；有界 L0/L1 上下文。未知 role/intent 保持 null。 |
| 浏览器状态 | 特征树、参数编辑、已提交模型预览；完整 JSON snapshot 后接有序 state delta 和新 STL 引用，重连恢复；相机操作不创建内核任务。 |
| 多人审阅 | 单次、限时邀请，跨账号 viewer，真实评论及在线心跳；后端拒绝 viewer 修改/导出。授权撤销后旧邀请不能恢复权限，排队操作也不能继续获准。 |
| 工程验证 | 复用几何、视觉、DFM 和已有审查提交流程；检查失败和未覆盖项保留为真实证据。 |
| 运行与部署 | API、独立 Worker、内核、Nginx 前端镜像实际构建；修复构建上下文、vendored 依赖、共享工作目录和 `/api` WebSocket 代理。 |

本轮还修复了真实测试暴露的操作 ID 重用、视觉模型默认值、凹面遮挡渲染、规划响应不完整、Temporal 中断清理影响新尝试和 Docker OOM 诊断问题。既有 CadQuery、ezdxf、装配/BOM、历史和 Fusion 合同保留并参加适用回归。

实现和接口说明见 [云文档架构](../cloud-documents.md)。

## 测试结果

| 测试层 | 方法 | 结果 |
| --- | --- | --- |
| 后端完整隔离回归 | 与 CI 相同的 test 环境，排除 docker/llm/fusion_e2e marker | **1269 passed、137 skipped、1 deselected**；最后一轮 38.04 s。跳过项不能算作通过。 |
| 前端 | ESLint、TypeScript、Vite build、Node tests | **88 passed，0 failed**；发布镜像中的 TypeScript/Vite 构建也通过。 |
| PostgreSQL / S3 / API | 独立数据库和 bucket 上的真实存储及控制平面回归 | **64 passed**；最终授权修复后云文档 **6 项再次通过**，其中 1 项是新增撤销回归，因此两批不是 70 个独立用例。 |
| Temporal / 内核集成 | 分批串行执行真实服务回归 | **17 项通过**：11 项主流程/修复/装配，5 项确认/取消/DXF/过期请求，1 项进程崩溃恢复。 |
| FreeCAD 专项 | 实际容器内核，跨执行任务 reopen / modify / replay / rollback、减材及边特征、语义边选择、原生 BOM、参数连续修改与改回 | **5 项通过**，其中连续参数修改专项在操作 ID 修复后单独复跑通过。 |
| 独立内核测量 | 真实已提交 FCStd 的只读副本，10 次打开、改孔径、重算、验证、回滚 | **通过**；属性赋值与重算中位数 **4.29 ms**，样本 P95 **6.04 ms**，原文件 SHA-256 不变。 |
| 真实模型服务端到端 | 新账号 → WebSocket → Agent → Temporal → FreeCAD → S3 → 审查/提交 → 参数修改 → 状态同步/共享 | **通过**，没有服务替身或固定模型结果，见 [服务证据](cloud-document-p0/live-service-final.json)。 |
| 真实浏览器端到端 | Chromium、真实 WebGL，分别测试 Vite 和 Nginx 发布入口 | **通过**：编辑、确认、提交、刷新恢复、双账号邀请/评论/在线状态、390 px 布局；页面 JavaScript 异常为 0。见 [发布入口证据](cloud-document-p0/packaged-browser.json)。 |
| 兼容与静态检查 | Fusion schema generator `--check`、`git diff --check` | 通过；远端 CI 未触发。 |

Temporal 的确定性故障注入测试控制了部分 LLM 返回，以复现旧 CadQuery 历史及失败场景；数据库、Temporal、S3、内核执行和崩溃恢复均为真实服务。这类测试与真实模型服务端到端分别记录，不能把测试中的受控 provider 称为真实模型调用。4 个需显式开启的其他真实 provider 专项未纳入这 17 项统计。

实际服务环境使用独立测试库 `cad_native_p0_test`，回归使用另一测试库 `cad_native_p0_regression`。未重置项目原有工作区；开工保留的 513 个基线文件无缺失。

## 实际模型与门禁证据

通过 Nginx 入口生成 60 × 40 × 8 mm 板和中心 6 mm 通孔，获得 14 个内核对象；随后真实参数操作改为 8 mm。FCStd、state、STL 下载均核对 SHA-256；特征 ID 保持稳定，delta 包含 3 个受影响对象。重放同一请求不会新建工作流，旧 state/revision 请求返回 409，viewer 几何写入返回 403。

生成工作流：`78b86448-0738-4bb7-bf4e-69967b0c72bf`。参数修改工作流：`5b134466-3fbe-49de-b28c-5542dc4a64b9`。数据库和产物中的真实门禁摘要见 [gates.json](cloud-document-p0/gates.json)。

- 几何 required gate：通过。
- 真实视觉 provider：通过；没有标尺的渲染不能证明精确孔径，尺寸依据内核 state 与参数验证。
- DFM advisory gate：**failed**，保留 `fdm_bridge_distance`、`fdm_min_feature` 未计算的事实及悬臂提示。工作流完成表示产物和审查候选已生成，不能解释为全部制造规则通过。

在此前真实浏览器修改工作流 `a7480c30-7ee1-4e05-b03b-b21a46c96f06` 中，内核执行步骤记录为 1939 ms，几何检查 7050 ms，DFM 2501 ms。这些是独立沙箱步骤耗时，包含进程启动和导出，**不是纯 recompute 基准**。

另在相同固定内核镜像中，对真实 60 × 40 × 8 mm 板的 8 mm 孔做 10 次独立打开及 8→9 mm 修改；属性赋值与 `recompute` 中位数 4.29 ms，样本 P95 6.04 ms。每次验证形体有效、体积减少、事务回滚后孔径恢复，并验证原 FCStd SHA-256 不变。容器限制 2 CPU、1 GiB、无网络；这组小样本只代表该简单零件，不能推导大装配或高并发性能。原始测量见 [kernel-timing.json](cloud-document-p0/kernel-timing.json)，脚本见 [freecad_kernel_timing.py](../../backend/tests/e2e/freecad_kernel_timing.py)。相机拖动的新增内核操作数为 0。

## 发布形态与复现

实际构建镜像：

- 后端 `localhost/cad-agent-backend:cloud-p0-final-20260908`，image ID `sha256:f486cb70e973ee1f5577dbae344e435dec8d18663a10efe3b0784cdb9974487f`。
- 前端 `localhost/cad-agent-frontend:cloud-p0-final-20260908`，image ID `sha256:52df641dae97812dbb83d192e84743e08672162d689aba5dd549d6a4834594cd`。
- 内核 `localhost/cad-agent-sandbox:cloud-p0-20260908`，image ID `sha256:a18600a30d598cb9084f42817d7f9fe849436c255152e0e723ec54e4bf0b8db9`，实际 FreeCAD 1.1.3。

打包 API 和 Worker 未挂载项目源码。Nginx 发布入口为本机 `http://127.0.0.1:8087`；这是隔离验收环境。部署步骤见 [DEPLOY.md](../../DEPLOY.md)。由于全新依赖下载出现超时，本机后端构建使用已有相同固定依赖镜像作为 `PYTHON_IMAGE`，当前源码仍重新打包；不声称已验证空缓存联网构建。

后端隔离回归命令：

```bash
cd backend
APP_ENVIRONMENT=test DURABLE_CONTROL_PLANE_ENABLED=false \
python -m pytest -m "not docker and not llm and not fusion_e2e" -q
```

完整服务及浏览器脚本使用真实凭据和独立测试数据，配置及账号文件不得提交：

```bash
cd backend
CAD_NATIVE_E2E_URL=http://127.0.0.1:8087 \
CAD_NATIVE_E2E_ENV=/private/path/test.env \
python tests/e2e/cloud_document_acceptance.py

CAD_NATIVE_E2E_WEB=http://127.0.0.1:8087 \
python tests/e2e/cloud_document_browser.py
```

浏览器脚本需要 Python Playwright 和 Chromium。服务脚本输出的私密 session 文件供浏览器脚本使用。两者支持 `CAD_NATIVE_E2E_PRIVATE` 指定同一私密路径；报告路径可分别通过 `CAD_NATIVE_E2E_REPORT`、`CAD_NATIVE_BROWSER_REPORT` 配置。PostgreSQL/S3/Temporal 回归的依赖配置见 [开发文档](../development.md)。

截图：[已提交状态恢复](cloud-document-p0/01-restored.png)、[真实修改提交](cloud-document-p0/02-reviewed-commit.png)、[跨账号审阅](cloud-document-p0/03-shared-review.png)、[移动端](cloud-document-p0/04-mobile-review.png)。

截图中的操作日志保留了修复前的失败记录。最终发布镜像上的浏览器操作为 `d5f0741e-e830-4bc4-9f4b-0387c6753a48`，孔径已提交至 12 mm，文档 `state_version=6`；对应几何和视觉门禁通过。

## 尚未完成或无法确认

- P1 的特征租约、并发操作 rebase/语义合并、局部网格刷新和 LOD；当前按文档串行并明确拒绝冲突，整体替换 STL 网格。
- P2 的完整 CAE/CAM/PLM/硬件工程平台，以及真实 Fusion 桌面、Onshape 和打印机等外部系统验收；既有合同或单元回归不能替代这些系统验收。
- 完整工程意图标注、任意 L2 状态补读工具循环、外部对象重命名后的语义身份迁移，以及所有 DFM 规则覆盖。
- DB 已提交而 Temporal 尚未启动的间隙沿用客户端以原幂等键重试；没有后台待派发扫描器。每次内核任务重新打开 FCStd，没有驻留内核池。
- 高并发、复杂大装配与长时间稳定性未验证。文档 WebSocket 当前按秒轮询；操作、评论和在线列表有数量上限。前端 3D chunk 约 908 kB，仍有构建体积警告。
- 本机 Podman VM 约 4 GB，测试 API/Worker 与多个内核叠加时曾实际 OOM；串行化回归后崩溃恢复通过。主机与 VM 时钟有偏差，真实 S3 调用放在 VM 内完成；未改变用户原有服务或系统时钟。
