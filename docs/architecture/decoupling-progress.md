# 模块解耦实施与阶段验证

基线：`62084d5c84ead7bcd88030695fdfcd61112280ea`。工作分支：`refactor/module-boundaries`。
实现目录：`/Users/wentao/CAD-Agent-decouple`，保留原工作目录及其未提交文件。

用户授权连续执行所有阶段；每阶段运行专项，再进行完整回归。代码与本地验证已经完成，GitHub 强制分支保护受套餐限制，不能称为全部发布完成。

## 阶段记录

| 阶段 | 已实施内容 | 当阶段验证 |
| --- | --- | --- |
| 1 | 模块责任与公共入口、真实导入边界、违规基线、专项 JUnit 门禁、汇总 Gate；固定旧版数字/Job/原生协议样例 | 边界正负例；真实模型/监控 PG 与 Temporal 16 项；协议/数值专项；后续补齐黄金样例、迁移、CI 浏览器与回放 |
| 2 | 按业务提取前端 Clients；模型操作契约、Handler 注册、事务栅栏、UsageSink 与生产装配；移除 Worker 对 SDK 私有生命周期字段的依赖 | 后端 1527 项；真实数据库/Temporal/Worker 专项 25 项；前端 152 项、lint、build |
| 3 | 面板范围参数 Controller、WebSocket 业务分发、认证服务、共享 DTO、文档受理用例、监控查询服务与可独立配置的只读连接 | 前端 154 项；真实参数 6→6.5 mm、拒绝保留输入、候选保存、相机/缓存/布局；监控浏览器与独立只读角色专项 |
| 4 | 原 Activity 大文件拆为规划、生成、执行、验证、BOM、候选、工程与生命周期 Handler；保留 Activity wire 包装及名称 | 后端 1527 项；真实数据库/规划 13 项；原生约束、参考几何、FEA/CAM/发布协议；真实 100×60×4 mm 生成；最终补齐 6 份旧/新历史回放 |
| 5 | 通用任务创建不再隐式入队文档；显式受理用例保持同一事务；移迁全部生产写入入口；兼容入口与 CI 收尾 | 事务专项 41 项后扩展到 80 项；真实核心集成 30 项；真实 Provider 5 项；其余最终结果见下表 |

阶段计数是各轮执行记录，不能相加当作独立测试总数。中间失败均保留日志，最终结论使用修复后的完整运行结果。

## 最终验证

| 层级 | 结果 | 实际范围 |
| --- | --- | --- |
| 后端通用回归 | 1535 passed；180 skipped；1 deselected | 单元、契约、错误/权限/状态/协议等。跳过项不计通过，真实依赖专项另列 |
| 前端 | 154 passed，lint/build 通过 | 客户端、任务与会话归属、参数/候选/版本行为；构建仍有原有大 chunk 提示 |
| PostgreSQL 其余专项 | 80 passed，零跳过 | 事务、租户、权限、幂等、队列、Head/CAS/ABA、制品、事件、撤权与分发 |
| 模型 Job | 11 项长等待专项 + 1 项续跑专项通过 | 310 秒保持执行且只调用一次；取消、旧代次拒绝；500 次以上真实 Temporal 轮询后 continue-as-new 仍为同一 Job/单次操作。续跑测试仅加速测试 Worker 的轮询计时 |
| 监控 | 6 passed，零跳过 | 管理员授权、并发归属、未知/取消用量、真正独立 DB 登录身份无法提权/读凭据/读请求/写表 |
| 迁移 | 1 passed，零跳过 | 从 0028 升级到 0029/0030，旧任务、排队 Job、未结束调用保留；不改写已应用迁移 |
| 核心服务集成 | 30 passed，5 个真实 Provider 用例另选运行 | PostgreSQL + MinIO + Temporal + FreeCAD/CadQuery；WebSocket 重放、修复、装配、取消、Worker 进程崩溃、旧 REST、2D 导出 |
| 真实 Provider 集成 | 5 passed，零失败 | 原生建模与修改、视觉证据、修复生成、规划/检索/代码生成、旧 REST 生成链；其余受控用例不混称真实 Provider |
| Workflow Replay | 6 份历史、4 类 Workflow 通过 | V2 模型 Job patch 前后、旧后端政策；V1、Check、ModelJob |
| HTTP 契约 | 与基线完全一致 | 137 个路径、111 个 schema 的 OpenAPI 比较 |
| 原生内核 | 通过 | 两种半径的正常/欠约束/冗余/冲突 8 案例；保存重开参考几何；真实 FEA/CAM/发布结果文件、哈希、源模型不变 |
| 浏览器 | 通过 | 原生候选、孔径 6→6.5 mm、拒绝保留草稿、版本/缓存/相机、320–1440 px 布局、独立监控双账号；新增 CI 启动到退出脚本实际演练 |
| CAM 浏览器完整链 | 通过 | 真实界面设置→原生计算→192 段刀路播放→NC/证据导出；过短刀具真实失败、幂等和 Head 不变 |
| FEA 完整服务链 | 通过 | HTTP→分发→Temporal→FreeCAD/CalculiX→S3→版本证据；4065 节点，力平衡相对误差约 4.47e-9；缺失部件真实失败，Head 不变 |

## 已发现并处理的问题

- 拆分时工程/场景 Handler 漏接 `backend`：修正显式依赖，增加所有 Activity 包装的参数绑定测试，重新验证真实场景和工程链。
- 旧测试修改旧文件全局变量：迁到实际实现位置，保留原断言。
- 布局脚本把前端模块 `onshape.ts` 请求误当成 Onshape API：限定真实 `/api/` 请求；历史选择改按明确 Revision 身份。
- 测试迁移地址、运行地址和 Temporal 队列曾混用：独立库、明确迁移环境和独立队列后整套重跑；无效轮次不计通过。
- 真实 Provider 开关污染受控用例：按用例隔离凭据；视觉夹具的“两孔支架/无孔长方体”矛盾与必然等待确认的假设已修正，未放宽生产验收规则。

## 边界与未完成事项

- **GitHub 分支保护尚未生效**：管理员权限存在，但私有仓库 API 返回 403，要求 GitHub Pro 或改变可见性。没有擅自升级付费套餐或公开仓库；失败 PR 禁止合并的验证也未完成。
- 本次没有部署腾讯云，未声称云端升级或监控镜像回滚验收。数据库 schema 不变，旧应用兼容入口保留。
- 外部 Fusion/Onshape 桌面、真实设备/机床、手机实物适配不在本轮真实验收内；契约测试不能替代这些环境。
- 未将单体强拆为微服务，也未把所有 Store/路由拆完；保留合理现有模块，限制未来越界。导入检查不能证明全部动态依赖或 SQL 数据责任。

## 证据与复现

私有完整日志：`/private/tmp/cad-decouple-20260920`。关键报告：`final-backend.xml`、`final-postgres.xml`、`final-integration-core.xml`、`final-live-all.xml`、`final-monitor-db.xml`、`migration-final.xml`、`model-rollover.xml`、`final-model-longwait.xml`、`final-replay-complete.json`，以及 `final-candidate/`、`final-layout/`、`final-rejection/`、`final-monitor-browser/`、`native-ci/`、`final-engineering/`、`final-cam/`。

CI 内置隔离数据库/Temporal/MinIO、原生镜像与浏览器启动；`scripts/ci/require_test_report.py` 拒绝缺报告、零测试、跳过或失败。真实 Provider 密钥不写仓库，真实 Provider 测试证据单独记录。架构说明与运行/撤回方式见 [module-boundaries.md](module-boundaries.md)。
