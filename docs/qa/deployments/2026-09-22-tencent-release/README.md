# 2026-09-22 腾讯云分支部署

本次按用户明确要求发布新分支，已部署并建立一个持久化普通账号。公网登录、真实 Provider → Temporal → FreeCAD → 候选接受/提交 → 工件下载 → 浏览器恢复模型均已实际通过。没有合并 main，也不将此次部署等同于 F02 的合并保护验收通过。

## 发布身份

- 分支：`release/tencent-20260922-acceptance-fixes`。
- 应用源码：`af0a090bd7acbd48a299617eb9407fd562976393`。后续部署报告提交只增加证据，不改变运行应用。
- CI：[35680964965](https://github.com/OwenYWT/CAD-Agent/actions/runs/35680964965)，五个必需任务全部成功。
- 目标：`129.226.74.100`，公网 `https://www.wordswave.ai`，Compose 项目 `cad-native`。
- 发布目录：`/www/releases/cad-20260922-acceptance-af0a090`。
- 数据备份：`/www/backups/cad-20260922-acceptance-af0a090`，包含应用、Temporal 数据库及旧配置；只在服务器保存，未入库。
- Schema：`0030_model_job_json_numbers`，此次未变更生产 schema。

| 运行组件 | 实际镜像 digest |
| --- | --- |
| backend / workflow-worker / monitoring | `sha256:f7f59817f49bddbbc631b3a411215f69fd44cbeadc0e4b4d9b25da6bcbeab117` |
| FreeCAD sandbox | `sha256:94245a1c0b3d3d393fa9d3decfc2fa42c90982e7179ce87e649661c7d5385f03` |
| frontend | `sha256:704569d3f918ef21c07e7a7c6d10a3b777cfe243cfc74e2b67739fbac1df2092` |

使用服务器已有、按 digest 固定的 AMD64 运行时层，核对依赖文件后替换完整应用源码。镜像内 834 个 backend 源文件、472 个 sandbox 源文件及全部前端构建工件与上传清单一致。旧镜像、旧配置和持久化卷保留；切换前确认没有在途任务。没有增加业务输出 token 限制或模型调用总时限。

## 实际验证

| 验证 | 结果和边界 |
| --- | --- |
| 云端原生几何 | 实际发布 sandbox 镜像：两组尺寸、outer / hole_mouths / all 三种范围、旋转多孔、不支持曲面拒绝全部通过。独立最终实体和 STEP 检查。 |
| 新旧镜像升级/回退协议 | 在同一云主机的独立数据库 `cad_release_acceptance_20260922`，真实旧/新 backend 镜像交替执行 seed、接管、过期写入、验证；升级和回退两个方向通过。未在生产中注入故障。 |
| 新旧监控组合 | 真实旧、新 monitoring 镜像分别连接上述独立数据库，匿名 401、普通用户 403、管理员 200；拒绝敏感字段和写入。 |
| 正式监控 | 独立 DB 登录 `cad_monitor_reader_20260922`，查询时角色 `cad_agent_monitor`。实际正式服务匿名 401、新账号 403；敏感字段、无行写入、提升到 runtime 角色四种操作被拒绝。认证仍使用原认证存储，查询连接单独只读。 |
| 公网浏览器 | Chromium 经 HTTPS 实际密码登录，刷新保持会话；重新打开已保存模型，加载正确 revision 和真实 WebGL，页面错误为空。测试固定域名解析到目标 IP，TLS 证书校验未关闭。 |
| 账号持久化 | 通过认证服务创建，数据库存在真实 auth_users 记录，实际密码登录通过；普通用户，无平台管理员权限。凭据未入库。 |
| 最终健康 | backend / worker / frontend / monitoring 运行镜像与清单一致；backend readiness、monitor health、frontend 均返回 200。 |

### 实际 Provider 建模样例

需求为 100 × 60 × 10 mm 板、中心 Ø8 通孔、所有外边 1 mm 倒角，导出 STEP 和 STL。使用正式 Provider，无预制响应或替代模型。任务包含一次自动修复，最终成功并提交；并非声称所有首次生成均能直接成功。

- Workflow：`e0a4d5ab-d7b2-48a0-8cee-0cc77d5b123b`。
- Document：`7765e788-b122-44b8-9f9e-53c85494f37f`。
- Revision：`ca0eee8f-a1c3-4c02-a6a9-fcb32f5b0950`，state_version = 1，15 个特征。
- FCStd、网格、参数状态和 STEP 下载摘要均与服务记录一致。
- 独立参考实体与最终 Body Tip、重新读入 STEP 的差体积均为 **0 mm³**。
- 两个孔口均为 **Ø8 mm**，直孔深度均为 **10 mm**，只倒指定外边。
- 本样例保留在新账号历史项目中。没有验证未提出的实物配合、制造公差或材料性能。

`live-chain.json` 的 `public_url` 字段为建模脚本实际使用的服务器本机 API 地址；公网验证由两个 `public-*-browser.json` 单独证明，不能将本机 API 验证冒充公网验证。

## 回退方式与尚未关闭事项

1. 旧服务各自的 Compose 目录、旧镜像 digest 见 `evidence/deployment.json` 的 `previous`。在无在途任务时，用这些目录按原镜像重建 backend/worker、monitoring、frontend；数据库未升级，本次不需要降级 schema。生产库不能用测试数据库替换。
2. 实际新旧镜像已完成独立数据库协议演练；未执行一次完整生产栈回退，也未进行生产任务故障注入，不能声称这些已验证。
3. F02 仍受 GitHub 私有仓库套餐/权限限制，main 必需检查及独立审查保护尚未生效。本次是用户新授权的分支部署，不是合并 main 或解除该项遗留阻塞。
4. F06 的目标镜像、云端协议、监控组合与本次部署身份证据已补齐；最终 main 合并 SHA 和完整隔离栈故障回退仍不在此次证明范围。
5. 原主工作目录及测试团队未跟踪报告未覆盖。本次发布使用独立 worktree。

证据清单及摘要见 `evidence-manifest.json`。仅归档脱敏运行事实；环境配置、数据库备份、密码和登录 token 均不进入 Git。

归档文本日志只规范换行和行尾空白；原始日志保留在服务器发布目录。
