# 独立验收问题整改与复验

状态：已落实 F01–F04 等代码整改并取得下列真实调用链证据。**视觉预算耗尽负例尚未验证通过，不能宣布完整方案通过验收**；未实现能力与测试覆盖边界单列在文末。

依据：[独立验收报告](../../.gstack/qa-reports/acceptance-20260909-2235/验收报告.md)。此前 [融合交接报告](cloud-cad-fusion-2026-09-09.md) 的完成结论被独立验收中的需求反例推翻，不能作为当前完整方案通过的依据。

## 必须复验的范围

- [x] F01：确定性编译必须完整覆盖需求；四角四孔、偏置盲孔、不同直径多孔通过真实内核测量及实际任务链路。
- [x] F02 对应代码整改：明确设计不符阻断、汇总不再假通过、修复后重新验证、带风险接受必须明确审阅。真实修复覆盖视觉、DFM 和内核失败；视觉预算耗尽负例的验证层级见文末。
- [x] F03：平面选择器验证真实曲面类型，并拒绝歧义及过期版本。
- [x] F04：工程流程仿真入口接通已有真实工程任务界面，状态和结果随修订更新。
- [x] C3/C5：完善有事实来源的语义摘要、修订和拓扑绑定，未测量内容明确标注；以当前原生修订的有限解析器为范围，不冒充通用拓扑历史追踪。
- [x] 完成受影响模块回归、全后端/前端回归、真实服务和浏览器端到端复验。
- [ ] 视觉修复预算耗尽后的完整真实负例：三次尝试均未覆盖目标分支，原始失败与原因保留。

## 保护与边界

保留原始验收报告、证据、数据库和端口 8089 的验收环境；不覆盖已有未提交改动，不提交或发布代码。新增测试使用独立测试记录及受控环境，不让清表测试接触既有项目数据。

生产云、真实硬件、外部企业系统等缺少实际目标环境的项目，单列为未完成或无法验证；不凭本地回归结果宣称通过。

## 已取得的真实复验结果

隔离入口为 `http://127.0.0.1:8090`，数据库、对象桶和 Temporal 队列均使用 `cad-remediation-20260910` 对应的独立配置。数据库 pytest 另用 `cad_remediation_pg_20260910`，没有清理 E2E 数据。完整扩展回归已结束，以下记录只支持所列场景。

| 场景 | 本轮实际结果 | 证据 |
|---|---|---|
| 四角四孔 | 浏览器任务成功；FCStd、STEP 均为 80×60×8 mm，四个 Ø6 通孔，孔心 (10,10)、(10,50)、(70,10)、(70,50)，体积 37495.22131576613 mm³ | [独立测量](../../.gstack/qa-reports/remediation-20260910/all-hole-measurements.log)、[任务](../../.gstack/qa-reports/remediation-20260910/holes/four_corners/task.json) |
| 偏置盲孔 | 两种格式均测得 Ø6、中心 (20,20)、深 3 mm、底厚 5 mm | [测量](../../.gstack/qa-reports/remediation-20260910/all-hole-measurements.log) |
| 不同孔径 | 两种格式均测得 Ø6@(20,20)、Ø10@(70,50)，两个通孔 | [测量](../../.gstack/qa-reports/remediation-20260910/all-hole-measurements.log) |
| DFM 自动修复正例 | 真实薄底检出后调用真实 provider 修复：底厚 0.3→1.0 mm，孔深 7.7→7.0 mm；外形、孔径、孔位保持正确；两轮 DFM，新工件重新验证并封装。浏览器审查提交后，FCStd 与独立测量文件字节一致 | [闭环](../../.gstack/qa-reports/remediation-20260910/dfm-repair-final/summary.json)、[最终测量](../../.gstack/qa-reports/remediation-20260910/hole-measurements-final.log)、[浏览器提交](../../.gstack/qa-reports/remediation-20260910/dfm-repair-final/commit-summary.json) |
| 视觉自动修复正例 | 浏览器提交明确的单孔初始操作清单；真实视觉发现中心单孔不符四角四孔标准，真实 provider 修复一次，第二轮视觉/几何及最终 DFM 通过。FCStd 与 STEP 分别实测单孔→四孔，尺寸、孔位、孔径、孔深、体积正确；浏览器审查提交后的原生文件与测量文件字节一致 | [闭环](../../.gstack/qa-reports/remediation-20260910/visual-repair-positive-v2/summary.json)、[来源与工件哈希](../../.gstack/qa-reports/remediation-20260910/visual-repair-positive-v2/staging/provenance.json)、[独立测量](../../.gstack/qa-reports/remediation-20260910/visual-repair-positive-v2/native-measurements.log)、[提交](../../.gstack/qa-reports/remediation-20260910/visual-repair-positive-v2/commit-summary.json) |
| DFM 预算耗尽 | 预算 1，真实检查两次均失败；修到用户允许的 0.4 mm 后停止。最终 advisory 风险为 warning/1 项；空意见接受返回 422，直接提交返回 409，主版本不变 | [浏览器/API](../../.gstack/qa-reports/remediation-20260910/dfm-budget/summary.json)、[几何测量](../../.gstack/qa-reports/remediation-20260910/dfm-budget-measurements.log)、[风险界面](../../.gstack/qa-reports/remediation-20260910/dfm-budget/risk-review.png) |
| 不可能圆角 | 8 mm 厚板要求所有外边半径至少 4.5 mm，真实内核失败；一次真实修复后同类失败再次出现，重复失败保护提前终止；无可审查候选、无导出工件、主版本未推进 | [失败闭环](../../.gstack/qa-reports/remediation-20260910/repair-failure-v2/summary.json) |
| 原单孔反例的视觉判断 | 按原任务 SHA-256 核验四张渲染图，再调用真实视觉模型，仍明确判定单个中心孔不满足四角四孔要求；这是 provider 边界复验，不冒充新建模流程 | [原图复验](../../.gstack/qa-reports/remediation-20260910/visual-counterexample/report.json) |
| 拓扑选择 | 最终沙箱拒绝圆柱侧面、同向歧义平面、过期修订；真实方块平面通过 | [原生测试](../../.gstack/qa-reports/remediation-20260910/native-final-freecad_topology_acceptance.log) |
| 主流程仿真 | 浏览器真实提交 CalculiX 求解、显示场结果并下载证据；重开工程能恢复状态；更新 CAD 修订后不沿用旧的完成状态，旧结果标记历史修订 | [求解](../../.gstack/qa-reports/remediation-20260910/simulation-main-engineering-browser.json)、[修订测试](../../.gstack/qa-reports/remediation-20260910/simulation-revision/summary.json) |
| 取消及故障恢复 | 生成阶段取消在 0.564 秒到达 cancelled，之后 20 秒无迟到候选/工件。早期中断实验按真实 Temporal FAILED 状态经应用状态机收敛，原失败历史保留 | [取消](../../.gstack/qa-reports/remediation-20260910/provider-cancellation/summary.json)、[收敛记录](../../.gstack/qa-reports/remediation-20260910/interrupted-workflow-reconciled.log) |
| 精确参数失败 | 浏览器把真实 Fillet.Radius 0.5→4.5，内核报告自交；自动修复次数为 0，无候选/部分工件。错误在界面显示，刷新后仍为 0.5；已提交 FCStd/state/mesh 哈希全部不变 | [完整失败路径](../../.gstack/qa-reports/remediation-20260910/exact-parameter-failure/summary.json)、[界面](../../.gstack/qa-reports/remediation-20260910/exact-parameter-failure/failure-visible.png) |
| 真实模型服务超时 | 最后一条负例在需求提取阶段实际超时，最终失败。浏览器重开后从“查看任务”读取真实错误，无审查按钮、候选或工件，文档版本保持 0；这是超时失败路径通过，不计视觉预算耗尽通过 | [实际任务](../../.gstack/qa-reports/remediation-20260910/visual-budget-negative-v3/task.json)、[浏览器重开](../../.gstack/qa-reports/remediation-20260910/visual-budget-negative-v3/reopened-failure.json)、[失败详情](../../.gstack/qa-reports/remediation-20260910/visual-budget-negative-v3/failure-reopened.png) |
| 语义与 Agent 上下文 | 真实 provider 上下文哈希匹配持久化记录；L0 包含实测 60×40×8 mm、孔径 9.25 mm、同修订 DFM 及 FEA 引用；变更与未变更特征历史分别核对 | [来源追溯](../../.gstack/qa-reports/remediation-20260910/live-database-semantic.log) |
| 发布的拓扑绑定 | 从实际已提交 FCStd 逐一解析版本 3 投影：18 个真实平面、5 条真实圆边全部通过，旧修订全部拒绝 | [原生逐项核对](../../.gstack/qa-reports/remediation-20260910/semantic-bindings/native-v3.log) |

DFM 默认属于 advisory 检查；上述预算耗尽结果允许人工阅读风险后明确接受，不等于制造验证通过。设计明确不符及 required gate 未通过会阻断候选。不能把历史失败证据删除后宣称通过：失败轮次保留在任务历史，最终审查只引用与最终工件一起封装的证据。

视觉默认保留 advisory；自主 3D 任务不能关闭视觉检查，且至少有一次修复预算。**视觉明确失败不因 advisory 而放行**，预算耗尽后任务失败。截图无法判断细小特征或数值尺寸时保留 indeterminate，汇总为需明确审阅的 warning；显式 required 的视觉检查遇到 indeterminate 仍阻断。这不是几何检查通过就宣称满足全部设计要求。

## 复验中补充修复的问题

- FreeCAD `Shape.check()` 会以 `ValueError` 抛出真实几何错误；原来变成 internal，绕过修复。现在在形状检查边界保留 `shape_check_failed` 并归为 `cad_kernel`，未知 Python 错误仍归 internal。
- 视觉检查不能用无标尺截图证明毫米值或文件导出。新增明确的无法判定响应，保留原始意见和 provider 来源；可见缺孔等矛盾仍失败。视觉通过不代表所有尺寸已做数值验证。
- DFM 射线起点在表面外 0.01 mm，原计算把该距离算进壁厚，使 0.795 mm 被当成 0.805 mm。已扣除此偏差；0.795/0.800/0.805 mm 三个真实边界模型分别失败/通过/通过。无法测量时不再用包围盒代替局部壁厚。[原生证据](../../.gstack/qa-reports/remediation-20260910/native-final-dfm_geometry_acceptance.log)
- 仿真记录原先依赖打开检查器才读取，已改为工程页面独立读取；记录读取失败不会显示已完成。
- 协作 HTTP 测试现在像真实浏览器一样调用正式 API 续租，保持原有服务端租约时限；浏览器测试显式填写审阅意见，以覆盖新的风险确认流程。
- 真实协作提交曾因 Temporal 无活跃轮询器返回 500。现返回带重试说明的 503，真实空队列复验确认没有新任务、没有推进 head。[服务边界证据](../../.gstack/qa-reports/remediation-20260910/worker-unavailable-live.json)
- 真实 Agent 小圆角任务暴露本轮加严策略的回归：0.5 mm 圆角无法从当前截图可靠辨认，却因所有视觉检查一律 required 而失败。已改为上述分级策略，原始失败保留；明确设计矛盾和显式 required 条件保持阻断。[原始任务](../../.gstack/qa-reports/remediation-20260910/inspection-indeterminate-task.json)、[策略回归](../../.gstack/qa-reports/remediation-20260910/visual-policy-after.log)
- 精确参数输入与原生编辑同样不得授权模型自行改写；新增参数失败保护，保留实际内核错误。针对性回归及上表的真实浏览器无效圆角参数复验均通过。
- 逐项原生解析发现坐标基准平面的显示 Shape 被发布成实体面绑定，实际 `getSubObject` 无对应面。现在排除基准/坐标参考对象，采用投影版本 3；数据库版本 1/2 的不可变缓存保留，真实物理绑定全部重验。[最初失败](../../.gstack/qa-reports/remediation-20260910/semantic-bindings/native-diagnostic.log)、[升级回归](../../.gstack/qa-reports/remediation-20260910/pg-projection-v3.log)

## 已完成的回归组

| 检查 | 当前结果 |
|---|---|
| 后端常规回归 | [1374 passed、147 skipped](../../.gstack/qa-reports/remediation-20260910/backend-final-regression-v5.log)，含跳过原因 |
| 真实 PostgreSQL / MinIO / Temporal | [完整组 117 passed、5 skipped](../../.gstack/qa-reports/remediation-20260910/pg-temporal-combined-final-v2.log)；其后投影版本 3 的受影响完整数据库模块 [8 passed](../../.gstack/qa-reports/remediation-20260910/pg-projection-v3.log) |
| 真实 provider 条件集成 | 上一行默认跳过的 5 项单独启用后 [5 passed、20 deselected](../../.gstack/qa-reports/remediation-20260910/real-provider-integration-final.log)，覆盖 REST/Temporal 生成、FreeCAD 融合、视觉、修复和规划来源 |
| 前端测试 | [98 passed](../../.gstack/qa-reports/remediation-20260910/frontend-final-regression.log) |
| 前端 lint / 生产构建 | [lint](../../.gstack/qa-reports/remediation-20260910/frontend-final-lint.log)、[构建](../../.gstack/qa-reports/remediation-20260910/frontend-host-build-dev4.log)通过；保留大 bundle 提示 |
| 真实 FreeCAD runtime | [4 passed](../../.gstack/qa-reports/remediation-20260910/native-runtime-final.log) |
| 全部可用原生执行门槛 | [17 passed、48 deselected](../../.gstack/qa-reports/remediation-20260910/native-all-gates-final.log)，含实际 CadQuery/build123d、FreeCAD、能力家族、文件/网络/进程拒绝、超时/内存失败归一化、减材连续性及真实 provider L2 查询 |
| 原生拓扑 / FEA / CAM / BOM-release / DFM | [5 个脚本均退出 0 且输出明确成功标志](../../.gstack/qa-reports/remediation-20260910/native-final-results.json) |
| 镜像与源码 | [API/worker 226 个应用文件、沙箱 19 个文件、前端 9 个资源均一致](../../.gstack/qa-reports/remediation-20260910/image-source-verification.json) |
| 扩展 HTTP / 浏览器 / CLI | [20 个功能脚本、5 个真实种子模型脚本最终均退出 0](../../.gstack/qa-reports/remediation-20260910/suite-results-normalized.json)，保留 [完整失败与重验历史](../../.gstack/qa-reports/remediation-20260910/suite-results.json) |
| 投影升级的浏览器回归 | [同一模型 v2→v3](../../.gstack/qa-reports/remediation-20260910/projection-upgrade/summary.json)：去除 6 个基准平面绑定，模型字节、参数、特征内容和修订历史均不变，实际模型及内核详情正常显示，无新增建模操作 |
| 来源及收尾检查 | 分支、FEA、CAM、工程 Agent、语义和发布/Bridge 六项数据库来源追溯通过；[无非终态工作流、无卡住的文档操作](../../.gstack/qa-reports/remediation-20260910/final-queue-audit.json)，`git diff --check` 通过 |

不同组会重复覆盖底层逻辑，不把测试数相加成独立功能数量。后端常规组的 147 个 skip 仍按 skip 记录；其中数据库、原生和 provider 条件门槛按上表单独启用，缺少 Anthropic 凭据或当前 Python 环境不支持的 Chroma 场景不冒充通过。部分 pytest 服务边界用例隔离了无关规划环节，不作为全链路无替身的证明；20 个扩展脚本及专项浏览器测试使用真实服务/文件，无请求拦截或伪造成功响应。

额外视觉专项共五次尝试，只有正例第二次覆盖目标分支并完成独立测量、审查提交；三次预算负例均未通过该目标断言，详见 [逐次结果](../../.gstack/qa-reports/remediation-20260910/visual-attempt-history.json)。纯文本初始用例曾因生成指令截断/无效而失败，正例改用浏览器提交明确的 typed 操作清单，不替换任何 provider 或 gate 返回值。最后一个超时任务的浏览器失败路径已单独复验通过。

最终队列检查为 48 个成功、14 个失败、2 个取消，非终态工作流与正在执行/排队的建模操作均为 0；这些任务数包括负例和失败尝试，不作为通过率。共享证据中的临时签名下载 URL 已去除查询参数并标注脱敏字段，模型文件、工件哈希、gate 与修复记录未改写。

数据库完整组结束后，最终两项产品变动仅为排除基准对象绑定和采用投影版本 3。随后重新执行整个后端回归、完整云文档数据库模块、真实绑定逐项解析及浏览器升级回归；其余扩展链路保留此前成功执行的证据，不写成同一镜像上的同时运行。

## 最终运行版本与复跑入口

- 工作区代码已更新，未提交 Git、未推送或发布；原 8089 验收环境及既有开发服务保留。本轮服务入口为 8090，API 为 8030。
- 后端镜像 `localhost/cad-agent-backend:remediation-20260910-dev8`；API/Worker 使用相同镜像。沙箱为 `remediation-20260910-dev4`，前端为 `remediation-20260910-dev4`。完整 digest 及文件一致性见 [镜像核对](../../.gstack/qa-reports/remediation-20260910/image-source-verification.json)。
- 数据库迁移头为 `0025_semantic_checkpoints`，当前投影版本为 3；旧版本 1/2 缓存保留。
- 后端复跑使用 `APP_ENVIRONMENT=test python -m pytest -q -rs`，前端使用 `npx tsx --test tests/*.test.ts`、`npm run lint`、`npm run build`。真实服务与浏览器脚本的环境、依赖顺序、独立数据要求见 [E2E 说明](../../backend/tests/e2e/README.md)。

本机 Podman VM 约 4GB，早期运行曾出现 OOM；本轮加入了仅在 VM 内使用的 2GB 临时 swap，未写入 fstab。后端采用已核验依赖镜像重新打包当前源码，依赖文件与基底一致；一次从可变 Python 基础镜像重新下载依赖的构建已中止并保留日志，不能声称已完成全新依赖环境的从零重建。测试命令曾误写 `APP_ENV=test` 而触发开发配置安全检查，纠正为 `APP_ENVIRONMENT=test` 后全量重跑通过；应用启动检查未放宽。

## 未完成或无法验证的部分

- **未实现**：Bridge 的 USB/Serial/CNC 实机适配、企业系统插件，以及后续 PLM 产品流程。当前 Bridge 的完整实测范围是配对、不可变发布文件交付、哈希/回执和失败恢复。
- **缺少目标环境，未验证**：腾讯 CVM/COS 部署、目标 4c16G 容量与远程 worker pool、Windows/AMD64，以及真实设备加工和企业系统连接。本地 Podman/MinIO 结果不能替代这些验收。
- **视觉预算耗尽未验证通过**：视觉失败→修复→重验→独立测量→浏览器审查提交正例已完成。预算耗尽阻断控制流已有单元回归，但完整真实负例仍缺证据：第一版在操作生成阶段失败；第二版 provider 未遵守“保留中心孔”的校准限制，去除了中心孔并修成符合最终标准的四孔候选，该候选未提交；第三版明确限定冻结初始操作后，在需求提取阶段实际超时。三次均不计预算耗尽通过。自然语言对修复方式的限定不能据此当成已验证的强制约束；结构化精确参数编辑已有禁止自动改写的单独保护与真实反例。DFM 字面预算耗尽负例和内核重复失败负例已有真实闭环证据。
- **能力边界**：拓扑绑定限于当前版本可唯一解析的物理平面/圆边，不提供任意曲面跨任意编辑的永久追踪；用途/意图无事实来源时保持未知。DFM 壁厚为采样测量，不承诺发现任意细小局部薄壁。有限刚体装配、线弹性静力 FEA、外轮廓 CAM 和本地文件交付不等于通用桌面 CAD/CAE/CAM 或生产性能认证。

此前“完整融合已完成”的报告已加历史说明；原独立验收报告与原始失败证据没有改写。本报告提供本轮可复核结果，不替验收团队宣布完整方案通过。
