# Cloud CAD 融合验收与交接

> 历史报告：下文保留当时的测试结果。后续独立验收发现需求丢失、错误候选审查、拓扑选择和仿真入口问题，推翻了整体完成结论；当前修复与复验状态见 [独立验收整改报告](acceptance-remediation-2026-09-10.md)。不能用本页的旧结果替代本轮验收。

核对日期：2026-09-09。方案为 `AI_Native_CAD.html`，结论依据当前代码、打包镜像和真实调用链。P0 及后续九个开发模块已接入原项目；外部设备、厂商桌面环境和生产云部署的未验证范围单列如下。

## 已落地范围

| 模块 | 实际行为及验收 |
| --- | --- |
| 云文档与原生编辑 | 自然语言 → 真实 provider → typed operations → 隔离 FreeCAD → 检查 → 审查提交 → WebSocket/模型更新。中心孔 6→8→9 mm 连续修改，特征 ID 保持稳定；刷新恢复已提交状态。 |
| 自动派发与恢复 | 数据库事务保存工作流、文档操作和派发记录，Worker 自动补发；幂等、取消、过期尝试隔离及实际 Worker 停启恢复通过。 |
| DFM、语义与详情 | 六种实际 STEP 几何验证壁厚、跨距和接触；用户意图/用途标注具有独立版本。Agent 在修改前查询有界内核详情，读取详情不创建建模任务。 |
| 协作编辑 | 两个真实账号编辑独立特征，依赖冲突拒绝，独立参数在最新 FCStd 重新执行并审查；真实 90 秒租约到期、角色降级、连接撤销和 viewer 导出限制通过。 |
| 组件与实例 | 三档原生网格、几何缓存、局部替换、原生实例创建/移动接入浏览器。102 个组件含 100 个 Link，仅两份网格、两次下载，实测约 1.54 秒显示；这是该验收模型的结果。 |
| 分支与合并 | 原生分支重开/验证/审查提交、共同祖先语义比较、独立参数合并、冲突拒绝、显式审查后采用源几何通过；目标分支标注保留。 |
| 草图交互 | 50 次指针移动产生零建模请求、零详情请求；半径 7 mm、圆心 (0, -3) mm 原生求解并提交。无解三角形约束被拒绝，没有自动改写尺寸或污染 head。 |
| 工程计算与 Agent | 实际 Gmsh/CalculiX 静力求解、场结果渲染；三层、192 段 GRBL 外轮廓路径、播放和 NC 下载。真实 Moonshot 调用读取冻结分析引用，将 Pad.Length 8→9 mm 并审查提交，旧分析继续绑定旧修订。 |
| 发布与本地交付 | 原生 `Assembly::BomObject`、STEP/STL、原始 FCStd、标注和同修订 CAE/CAM 证据组成不可变发布。实际客户端交付 14 个文件并逐项校验；真实 305 秒租约过期、API/客户端重启、重复交付、撤销和异常目录恢复通过。 |

调用链和接口边界见 [云文档](../cloud-documents.md)、[工程计算](../engineering-compute.md)、[发布与本地交付](../releases-and-local-bridge.md)。

## 测试结果与证据

| 验证层 | 结果 | 保存的证据 |
| --- | --- | --- |
| 后端隔离回归 | 1330 passed，144 skipped，1 deselected；跳过项不计为通过 | [后端结果](evidence/cloud-cad-2026-09-09/backend-regression.txt)、[跳过详情](evidence/cloud-cad-2026-09-09/backend-skips.txt) |
| PostgreSQL/MinIO/Temporal/内核集成 | 完整服务组 115 passed、5 conditional skipped；随后打开真实 provider 门槛，另 5 passed，覆盖这五项 | [真实服务](evidence/cloud-cad-2026-09-09/real-services.txt)、[真实 provider](evidence/cloud-cad-2026-09-09/real-providers.txt) |
| 前端 | 94 passed；TypeScript、ESLint、Vite 生产构建和 Nginx 镜像构建通过 | [前端结果](evidence/cloud-cad-2026-09-09/frontend-tests.txt) |
| 既有原生路径 | 5 项 FreeCAD/CadQuery/Assembly BOM 实际内核回归通过 | [原生回归](evidence/cloud-cad-2026-09-09/native-existing-regression.txt) |
| 全部可用原生/隔离执行门槛 | 17 项首次 16 通过、1 项真实模型未先查询详情而失败；修复后该项及对应完整端到端通过。覆盖装配导出、实际运行时、减材连续性、非法访问、超时和内存超限归一化 | [专项及修复结果](evidence/cloud-cad-2026-09-09/native-gates.txt)、[真实 L2 查询](evidence/cloud-cad-2026-09-09/native-l2.json)、[API 到浏览器](evidence/cloud-cad-2026-09-09/agent-inspection-http.json) |
| 原生工程数值/几何 | 梁解析解与力倍增/平衡、四种 CAM 轮廓的独立扫掠刀具相交检查、多实体/实例 BOM 与三实体 STEP、发布全部哈希通过 | [FEA](evidence/cloud-cad-2026-09-09/native-fea.json)、[CAM](evidence/cloud-cad-2026-09-09/native-cam.json)、[发布](evidence/cloud-cad-2026-09-09/native-release.json)、[DFM](evidence/cloud-cad-2026-09-09/native-dfm.json) |
| 最终打包版本浏览器 | 文档、协作、组件/大型场景、分支、草图、FEA、CAM、发布、Bridge，以及 API 重启后的原页面重连通过；各脚本无未处理页面错误 | [文档](evidence/cloud-cad-2026-09-09/document-browser.json)、[协作](evidence/cloud-cad-2026-09-09/collaboration-browser.json)、[大型场景](evidence/cloud-cad-2026-09-09/large-scene-browser.json)、[草图](evidence/cloud-cad-2026-09-09/sketch-browser.json)、[分支](evidence/cloud-cad-2026-09-09/branch-browser.json)、[FEA](evidence/cloud-cad-2026-09-09/engineering-browser.json)、[CAM](evidence/cloud-cad-2026-09-09/cam-browser.json)、[发布](evidence/cloud-cad-2026-09-09/release-browser.json)、[Bridge](evidence/cloud-cad-2026-09-09/bridge-browser.json) |
| 数据一致性与恢复 | 分支事务回滚、RLS、不可变来源/发布/交付回执通过；全部最终工程产物记录实际沙箱 digest；验收租户没有未完成工作流、排队 CAD 操作或交付，测试 Bridge 全部撤销 | [分支数据库](evidence/cloud-cad-2026-09-09/branch-database.txt)、[发布数据库](evidence/cloud-cad-2026-09-09/release-database.txt)、[实际运行时与队列](evidence/cloud-cad-2026-09-09/artifact-runtime.json) |
| 代理故障回归 | 42 项部署配置测试通过。实际改变 API 地址，保持 Nginx 运行，约 14.66 秒恢复就绪；REST 查询参数、会话 WebSocket 和已打开页面自动重连通过 | [配置测试](evidence/cloud-cad-2026-09-09/proxy-config-tests.txt)、[重启验收](evidence/cloud-cad-2026-09-09/proxy-restart.json) |

隔离回归和真实服务专项是不同层次，未相加宣称为不重复测试总数。144 条隔离跳过中，120 条服务测试和 17 条原生/模型门槛另行启用验证。另 5 条遗留进程内生成测试仍使用过时的 `ANTHROPIC_API_KEY` 门槛，未执行，不能据此声称验证了该旧入口；当前 REST/WebSocket/批量路径由 Durable 集成与浏览器测试覆盖。另 2 条可选 ChromaDB 专项受主机依赖限制跳过，默认 TF-IDF 路径通过。1 条按 marker 排除的是真实 Fusion 桌面验收。

早期原生数值专项在对应模块打包时执行；最终 API/Worker/前端/沙箱又完成真实 HTTP、浏览器、数据库和文件系统链路。成功、失败、取消和拒绝路径均检查实际状态与产物，没有把失败返回值改成成功以满足测试。

关键实测证据：

- [实际分析与 provider 上下文](evidence/cloud-cad-2026-09-09/engineering-agent-provider.json)：provider 为 `moonshot`、model 为 `kimi-k2.7-code`，保存来源产物及上下文哈希；[修改结果](evidence/cloud-cad-2026-09-09/engineering-agent.json)证明旧分析没有冒用于新修订。
- [通用操作来源](evidence/cloud-cad-2026-09-09/agent-inspection-database.json)证明最新 Agent 使用真实 provider 生成操作并正常提交；[加载完成后的原生圆角](evidence/cloud-cad-2026-09-09/agent-inspection.png)与记录的修订一致。
- [Bridge 恢复](evidence/cloud-cad-2026-09-09/bridge-http.json)：第二次领取完成交付，已有文件修改时间不变，旧确认被拒绝；[异常目录](evidence/cloud-cad-2026-09-09/bridge-failure.json)证明失败可见且修复后创建新交付记录。
- 截图：[大型实例](evidence/cloud-cad-2026-09-09/scene.png)、[草图](evidence/cloud-cad-2026-09-09/sketch.png)、[有限元](evidence/cloud-cad-2026-09-09/fea.png)、[加工路径](evidence/cloud-cad-2026-09-09/cam.png)、[发布](evidence/cloud-cad-2026-09-09/release.png)、[本地交付](evidence/cloud-cad-2026-09-09/bridge.png)、[移动端审阅](evidence/cloud-cad-2026-09-09/mobile.png)。

## 验收中发现并修复的问题

1. **旧数据导入回滚与新增文档外键冲突**：项目导入会建立空云文档，旧回滚直接删除项目失败。现在锁定项目，仅清理未使用的空文档；导入后已有检查点或其他协作数据时拒绝回滚，保持整笔事务和真实产物。三个真实数据库回归通过。
2. **旧 DFM 断言与实际几何结果不一致**：原测试把可验证的盒体固定预期为警告/失败。新几何测量全部规则均已评估且没有违规，按实际结果更新断言，并明确检查没有未评估规则。没有放宽生产门禁。
3. **API 重启后浏览器入口长期 502**：Podman 为重启后的 API 分配了新 IP，静态 Nginx upstream 仍访问旧地址。前端镜像改用官方 entrypoint 的容器 DNS 和有期限的动态解析。真实地址变化、浏览器/会话流恢复及 Bridge 的五分钟重领再次通过。
4. **共享 VM 内存不足**：完整服务测试曾被 OOM 杀死。保留用户既有服务，停止本任务的旧重复部署，使用独立测试队列并限制内核并发为 1 后，完整服务组和真实 provider 组通过。没有将中断运行计为通过。
5. **通用原生修改的查询阶段提示冲突**：首句要求直接返回操作计划，后文又要求先查询详情；重试还省略了被拒绝回复。现在明确区分查询与计划两个步骤，并保留回复及测量上下文。15 项契约测试、真实 provider 的 10→12 mm 原生修改和最新打包 API 的“详情查询 → 0.5 mm 圆角 → 审查提交 → 浏览器显示”通过。未放宽必须查询的门槛；原模型参数与特征身份保持不变。

## 最终运行版本

本机入口：[http://127.0.0.1:8087/](http://127.0.0.1:8087/)。API 同时保留 `8017`、`8018` 回环端口。三个入口 `/ready` 均返回 HTTP 200；数据库为 `cad_native_expansion_acceptance`，bucket 为 `cad-native-expansion-acceptance`，schema 为 `0024_local_bridge`。[部署实测](evidence/cloud-cad-2026-09-09/deployment.json)记录实际容器、产物摘要、静态资源和依赖状态。

| 服务 | 最终镜像 |
| --- | --- |
| API / Worker | `localhost/cad-agent-backend:cloud-expanded-final-agent-20260909`，`sha256:461a7056a855260c38a45f6f09f31cecb9b3bf70a4c5de57d423652483b06e78` |
| 前端 | `localhost/cad-agent-frontend:cloud-expanded-final-dns-20260909`，`sha256:23e3fce8cdfe1d81912480fb906261ecd27f08db1f5c09971c91f172713fa1d9` |
| 沙箱 | `localhost/cad-agent-sandbox:cloud-expanded-20260908`，`sha256:c3aca6c2e9f3669e60baafac2260b069f81a58ad3ea63115002e73c5b4f6099e` |

API/Worker 均运行镜像中的应用，未覆盖挂载应用源码。API 仅额外挂载只读验收脚本；[225 个应用目录文件哈希](evidence/cloud-cad-2026-09-09/backend-app-manifest.json)与实际镜像逐项一致。

后端从头联网安装构建曾因 PyPI TLS EOF 中断。最终使用 `backend/Dockerfile.reuse-dependencies`：核对已验证基础镜像与当前 `requirements.txt` 完全一致，运行 `pip check` 后复制当前全部应用代码。默认 Dockerfile 仍支持全新安装；这一网络条件下没有将失败构建写为通过。前端按常规 Dockerfile 完整构建通过。

三个最终容器为 `cad-native-expanded-api-final`、`cad-native-expanded-worker-final`、`cad-native-expanded-frontend-final`，设置 `unless-stopped`。本任务较早的验收容器已停止并保留，`cadreport20260906_*` 等用户原有服务未替换。配置和测试会话的私有副本位于 `/Users/wentao/.local/share/cad-agent/cloud-expanded-20260909/`，目录权限 `0700`、文件权限 `0600`；报告不包含凭据。

## 与方案的取舍及未验证范围

- 已提交 branch head、操作记录、不可变原生产物和校验过的投影共同构成权威链。JSON state 是可验证投影，不能脱离原始 FCStd 重建任意模型；没有改造为第二套版本系统。
- 组件级网格更新、三档 LOD 和原生实例已实现。未实现顶点级二进制 mesh delta、通用结构 CRDT、任意复杂装配约束合并，结构冲突需要显式审查。102 个组件的验收不能外推为任意规模性能保证。
- 草图本地预览覆盖直接圆/线尺寸，最终约束由 FreeCAD 求解。未记录或不支持的几何明确提示；没有完整浏览器 CAD 求解器或全离线 CAD。
- CAE 当前为单实体、小位移、均匀各向同性材料的线弹性静力分析。CAM 当前为 Z 向等截面实体的外轮廓分层加工，内部孔/口袋明确不加工。非线性、接触、热分析、通用多轴刀路不在已实现范围。
- Local Bridge 已真实验证 **macOS 本地文件系统交付**；客户端支持 macOS/Linux，Linux 主机部署与 Windows 客户端没有完成本轮实机矩阵。未提供具体 CNC/USB/串口、夹具、插件或企业挂载目标，因而没有实现或验证这些设备/厂商适配；不能将文件已送达当作设备已执行。
- Onshape 凭据未配置；Fusion 真实桌面、厂商 PLM 和物理加工没有验收。既有可选 Connector 的合同测试不代表外部服务通过。腾讯云/COS 生产环境、域名证书及生产负载也未部署验证；当前运行结果来自本机 Podman、PostgreSQL、MinIO、Temporal 和真实模型服务。
- Vite 仍提示部分 bundle 超过 500 kB；生产构建通过，本轮未进行无关的依赖或全站性能重构。

## 复现入口

基础设施、模型凭据、内核镜像、数据库迁移和独立测试队列按 [开发文档](../development.md) 配置。回归脚本在 `backend/tests`、`frontend/tests`；端到端入口与运行顺序见 [E2E 操作说明](../../backend/tests/e2e/README.md)。必须使用隔离测试数据库和 bucket；端到端脚本创建真实账号/文档，故障脚本会停启显式指定的验收容器。
