# 2026-09-23 验收反馈修复与复验

应用修复提交：`62859fa42ba75bffd3186550a25c0ec903b5854e`。发布分支 `release/tencent-20260922-acceptance-fixes`；PR #9 分支 `refactor/module-boundaries`。尚未合并 main，不能宣称正式交付关闭。

## 逐项核验与修复

| 问题 | 核验、修改及结果 |
| --- | --- |
| N01 拒绝候选后额外弹窗 | 真实复现。审查回调原来把拒绝后的数据刷新当成版本切换；现在按权威审查结果刷新，仅提交/回滚主动切换。相同视图不触发草稿离开保护，版本列表移除重复保护，真正跨版本操作仍有保护。真实浏览器分别强制 HTTP 刷新先到、WebSocket 草稿恢复先到，两种顺序均保留原输入、不出现额外弹窗；查看其他版本仍出现确认。 |
| N02 倒角保护范围误判 | 真实复现。现在分开解析处理范围和保护范围，支持中英文否定、排除、保持不变及并列否定；需求字段分别解析。冲突、双重否定及不支持的集合减法仍明确拒绝，绝不改成全部边。覆盖生成器、修复边界、真实 Provider、最终 Body Tip 和 STEP。 |
| 连带发现：Body 作为倒角/圆角目标 | 真实 Provider 返回 Body 目标后，旧实现可能将新特征反向引用整个 Body。现在先取得该 Body 当前有效 Tip，再添加特征；显式拓扑选择器不能隐式改绑，空 Body 拒绝。验证非默认名称、多 Body、圆角和三种倒角范围。 |
| F02 合并保护 | 再次实时查询，main.protected=false；保护接口 HTTP 403，私有仓库套餐不支持。尚不能启用强制检查/独立审查，也不能证明失败 PR 无法合并。需要仓库所有者升级套餐；未改公开可见性、未绕过合并。 |
| F06 完整栈升级回退 | 完整独立栈双向在途任务演练、再次升级、构建版浏览器及新旧监控组合已通过；记录了仿真内存差异，腾讯云原生资源与正式发布闭环仍未完成。 |

## 测试层次与结果

| 层次 | 命令/证据 | 结果 |
| --- | --- | --- |
| 后端隔离回归 | `APP_ENVIRONMENT=test python -m pytest -m 'not docker and not llm and not fusion_e2e' -q`，`backend-final2.xml` | 1600 passed，181 skipped，1 deselected。跳过不计为功能通过。 |
| 核心服务集成 | Temporal MCAD、WebSocket replay、change-set API、PostgreSQL files/history、API compatibility；`core.xml` | 30 passed；6 个真实 Provider 用例从该组合排除，相关中英文流程另跑。 |
| 真实 Provider 全链路 | `test_agent_v2_real_freecad_generation_validation_seal_and_commit`，`live4.xml` | 中英文 2 passed。真实需求理解→计划→生成→FreeCAD→对象存储→参数修改及倒角→验证→提交；检查最终实体和 STEP。 |
| 倒角语义/生成器 | `test_chamfer_scope_contract.py`、`test_freecad_operation_generator.py`，`edge-final.log` | 64 passed；契约测试中的录制响应不作为真实 Provider 证据。 |
| FreeCAD 原生反例 | `scripts/ci/native_contracts.sh`，`native-*.log` | 草图冲突/冗余/欠约束、参考几何、9 组倒角实体与 STEP、旋转多孔、Body Tip、拒绝不支持形状，以及 FEA/CAM/release 结果通道通过。 |
| 前端 | lint、Node tests、生产构建；`frontend-final-*.log` | lint/build 通过；154 tests passed。 |
| 真实浏览器 | `scripts/ci/browser_contract.sh`，`browser-*.json` | 5 组通过：候选/刷新、参数闭环及未知受理、取消/拒绝两种时序、布局、监控权限。 |
| 架构 | `python scripts/architecture/check_boundaries.py` | 通过；不等于 GitHub 已强制要求通过。 |
| 镜像源码 | `deploy/tencent/verify-images.py`，`new-image-source-verification.json` | 后端 834 文件、沙箱 472 文件 SHA-256 一致。 |

## 保留的失败与测试边界

- 先跑反例再修复：原 N01 出现草稿离开保护；原 N02 误判范围；Body 目标出现无效倒角引用。`edge-red.log`、`unchamfered-red.log`、`body-target-red.log` 保留反例。
- 扩展真实 Provider 验证先后暴露了实际措辞 `不倒孔口` / `hole openings unchanged` / `unchamfered` 和 Body 目标问题；增加对应回归后，中英文完整流程最终通过。不能据此声称任意自然语言都能可靠解析；不明确的范围仍需澄清。
- 一次后端测试未设置 `APP_ENVIRONMENT=test`，启动安全检查正确拒绝；按 CI 测试环境设置重跑得到上表结果，不更改应用启动安全规则。
- 独立完整栈使用实际腾讯云导出的旧镜像，数据、认证账号、数据库、对象存储、Temporal 均独立。主机为 ARM，AMD64 镜像通过 Rosetta 执行，并非腾讯云原生硬件复验。
- 首次隔离演练的在途 Job 已从 generation 1 恢复为 2，但几何验证发生 `execution_oom`，Docker 有真实 OOM 事件，未判整栈通过。进一步确认校验的持久化执行规格仍为 512 MiB，普通环境变量不能覆盖；第二次仍 OOM。之后仅在独立 daemon 中，对已核对摘要的沙箱容器使用 3 GiB 仿真内存额度，保留原规格和容器层调整记录。观测器在整栈容器消失时曾因错误大小写差异退出，该轮也未判通过。应用源码、正式执行规格和线上资源配置均不变，无新增任务超时或 token 限制。
- 本轮不声称 Fusion 桌面、物理装配或制造结果得到实测验证。生产应用仍是先前发布的 af0a090，未用本轮本地结果冒充线上发布。

本目录不包含账号密码、Provider 密钥、会话 token、签名下载 URL 或私有环境文件。

## 完整隔离栈证据

- 旧→新：任务 `d5558abd-c510-4546-ad4e-b634df205fa6`；新→旧：任务 `6bb0a2ea-d5f2-41e9-8a7c-895ae5a14d9f`。两次在真实 `agent_v2.requirements` Job running 后 SIGKILL worker，重建全栈；同一 Job generation 1→2，payload_hash 不变，任务最终成功并提交。
- 两次旧代次对仍在运行的新代次调用真实 `renew()` 均返回 false，见 `live-generation-fence.json`。未修改租约时间、数据库时钟或注入假成功结果。
- 保留专用卷，全栈 PostgreSQL、MinIO、Temporal、backend、worker、frontend、monitor 容器 ID 全部更新；schema 始终为 `0030_model_job_json_numbers`。回退后此前 FCStd/mesh/state 的 SHA-256 一致；再次升级后两份已保存模型及账号仍可读取。
- 构建版浏览器真实登录、打开两份模型、WebGL、属性/检查/版本/导出抽屉保持 Canvas、刷新保持版本通过；监控普通账号拒绝、管理员真实调用记录可见通过，见 `full-stack-browser.json`。浏览器脚本初次出现按钮定位歧义和未等待目标场景加载的测试竞争，修正测试定位/等待条件后通过，没有改产品代码。
- 新后端+旧监控、旧后端+新监控、新后端+新监控均实测匿名 401、普通账户 403、管理员 200；独立监控数据库角色 UPDATE 被拒绝。见 `monitor-image-matrix.json`、`monitor-read-only.json`。
- 实际 AMD64 新沙箱再次通过同一原生约束、参考几何、倒角最终实体/STEP 和 FEA/CAM/release 组合，见 `amd64-*.log`；与 ARM 原生测试交叉验证。
- `stack_drill.py` 等文件是本次实际执行脚本快照，保留原工作目录以便审计，非通用安装器。私有环境及凭据未归档；复验需要重新配置独立环境。

**结论：N01、N02 已修复并有完整回归；完整隔离栈功能性升级/故障/回退证据已补齐。F02 仍被仓库套餐阻塞，F06 的最终 main 合并版本和腾讯云原生正式发布闭环尚未完成，因此整体交付不能标为全部通过。**

测试完成后已停止本轮专用验收栈与独立虚拟机，保留数据卷、镜像及私有配置用于复现；既有开发 Podman 和腾讯云服务未停止。

归档日志仅统一换行并去掉行尾空白；原始日志仍保留在本机验收目录。原始/归档 SHA-256 对照见 `log-normalization.json`。

[构建版浏览器实测画面](full-stack-browser.png) 仅作为显示与保存状态证据；尺寸/倒角结论来自原生实体和 STEP 测量。

## 最终 GitHub CI

[Run 35758829094](https://github.com/OwenYWT/CAD-Agent/actions/runs/35758829094)：应用提交 `62859fa42ba75bffd3186550a25c0ec903b5854e`，PR 组合提交 `9c75459702b897c00793ee912733a6565041c773`。5 个 Job 全部成功，包括 Required regression gate。已下载并再次执行必需证据校验器，18 份报告全部满足要求；副本保存在 `ci/`，汇总见 `ci-summary.json`。

CI 核心集成 30、事务/RLS 80、模型生命周期 12、监控 6、迁移 1 全部零失败、零跳过；旧/新源码监控分别 5/6 项通过。浏览器两种拒绝响应顺序、原生几何、冻结旧历史、动态历史、新旧源码协议演练均通过。310 秒持续等待及超过 500 轮续接在 CI 实际执行，未缩短等待来换取通过。CI 全绿仍不解除 F02 平台保护阻塞和正式生产发布边界。
