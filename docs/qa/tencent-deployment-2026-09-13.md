# 腾讯云部署与双账户验证报告

日期：2026-09-13。受众：运维、研发、功能测试人员。

已把原生共同编辑版本和本轮部署修复合入 GitHub main，并上线到 **https://www.wordswave.ai**。两个普通测试账户已通过真实邀请注册流程写入 PostgreSQL，加入同一项目，且通过数据库、对象存储和应用容器重建后的登录、权限、版本与文件完整性验证。

**这是受控内测部署验证通过，不能解释为完整产品验收通过。** 线上真实 Agent 请求返回 `ProviderQuotaError`，目前 AI 生成/修改受模型服务额度阻塞；手动原生参数修改、审核提交、协作和场景计算通过。历史实机及公开多租户限制仍然存在。

## 1. 发布身份与变更

| 项目 | 实际值 |
| --- | --- |
| GitHub | [OwenYWT/CAD-Agent](https://github.com/OwenYWT/CAD-Agent/tree/main) |
| 部署应用 main | `9743344e0736d2f0e72931a734e5887856d88a41` |
| 构建源码 | `590642f90f6102e1776985b9cccebac5ce161a5b`，Git tree 与上述合并提交完全一致 |
| PR | [共同编辑及部署修复 #3](https://github.com/OwenYWT/CAD-Agent/pull/3)、[BOM 非 root 写入修复 #4](https://github.com/OwenYWT/CAD-Agent/pull/4) |
| 主机 / 架构 | `129.226.74.100`，Linux amd64，2 GB RAM / 4 GB swap |
| Compose / 发布目录 | `cad-native`；`/www/releases/cad-20260913-bae67ae/tencent` |
| 数据库迁移 | 原 `0025_semantic_checkpoints` → `0026_change_set_base_generation` → `0027_document_scene_tasks` |
| 后端 / Worker image ID | `sha256:1819253772c06511d27c41dde2932186d376e68461becb9f170a7b5c9fa2bf28` |
| 前端 image ID | `sha256:27c29afbd7a8cfc9402e2bf392cc18d3477dedb6605b93ce0d5e7561bb7d8815` |
| 沙箱固定引用 | `cad-agent-sandbox@sha256:4324c7ebfecc565c8660c030858b8d0f2cf91b548f94ac8b9662b74f83bed8d2` |

发布目录名称保留第一次构建的短 SHA；`source-manifest.json` 和 `tencent/release.json` 记录最终来源。报告与部署说明是后续文档提交，不冒充重新构建过应用。原站 `https://wordswave.ai` 的 `cad-agent` 容器、库和对象存储持续保留，新旧账号分别存储。

本轮除上一阶段共同编辑实现外，完成以下实际修复：

- CI 和 Compose 的 MinIO 改用官方 Quay 同版本镜像，解决 Docker Hub access denied。腾讯云实测拉取后的 MinIO/mc 摘要与原同版本镜像一致。
- 增加腾讯云前端 Dockerfile 专用 ignore，允许打包已测试的 `dist/`。实际容器中的 9 个应用文件逐一匹配构建产物，官方 Nginx 自带 `50x.html` 单列。
- 基于已核验的同版本 AMD64 沙箱更新 state projector；后端仅在 requirements 字节一致且 `pip check` 通过时复用依赖。新镜像实际核对后端 778 文件、沙箱 467 文件，均无差异；沙箱真实 Runtime probe 通过。
- BOM 导出不再覆盖 UID 1000 沙箱拥有的只读结果文件，改在 Activity 自己的临时目录生成带 provenance 的 JSON 并上传该文件。没有放宽权限或改变真实内核输出。

## 2. 两个持久账户与试用入口

账号和随机强密码的本机交接文件：

`/Users/wentao/.local/share/cad-agent/tencent-test-accounts.md`

文件权限为 `0600`，目录 `0700`；密码、令牌、私钥和真实部署 env 均未提交 Git。服务器备份目录及 `cad_data` 中另保留私密交接记录。两个账户通过真实管理员邀请码和注册 API 创建，不直接向数据库插入伪造用户。

| 账户角色 | 可测试行为 |
| --- | --- |
| 项目 owner，普通用户 | 查看、编辑、审核、提交、邀请协作 |
| 项目 editor，普通用户 | 查看、编辑、审核；最终提交被正确拒绝 |

共享项目：`5885c39c-6dd7-539c-9061-e28401619027`。
[共享原生文档](https://www.wordswave.ai/?document=dc0c59ad-633f-49ca-aff0-2821c01c3b11&workspace=178ffad0-9daa-55d1-97a5-66055cedc6dd) 已包含两个真实 FreeCAD Body，供修改参数、冲突租约、审核和提交测试。账户具体登录信息仅见私密交接文件。

## 3. 自动回归与实际云端调用链

[应用 main 的完整 CI](https://github.com/OwenYWT/CAD-Agent/actions/runs/34741834160) 三项作业全部成功。同源码 [PR #4 CI](https://github.com/OwenYWT/CAD-Agent/actions/runs/34741283307) 的完整日志汇总如下：

| 验证 | 结果与边界 |
| --- | --- |
| Python 3.11 后端常规回归 | 1,416 passed / 155 skipped / 1 deselected |
| Fusion 隔离合同专项 | 189 passed / 1 deselected；与常规回归重叠，不能相加 |
| 前端 | 113 passed；lint、TypeScript、生产构建通过 |
| 真实 MCAD / PG / S3 / Temporal 集成 | 28 passed / 5 skipped；完整固定 Runtime 构建与 probe 通过 |
| 非 root BOM 专项 | UID 1001 Activity 处理 UID 1000 内核结果，真实本机容器链 1 passed；同缺陷的 GitHub 真实集成复验通过 |
| 腾讯云来源检查 | 后端 778、沙箱 467 文件零差异；前端 9 应用文件哈希一致 |
| 原有数据 | 部署前既有 2 个业务文档 Head / generation 保持不变；原有账户保留 |
| 双账户协作 | editor 手改 PadB 10→11 mm、审核，提交 403；owner 提交成功；独立租约、冲突 409、越权释放 403、幂等重复请求和旧基线 409 均通过 |
| 真实浏览器修改 | owner 手改 PadA 10→11 mm，任务中刷新、审核提交、再次刷新；PadB 和稳定特征 ID 不变，FCStd/state/mesh 下载哈希匹配，页面/控制台错误均为 0 |
| 异步场景 | 两账户 4 并发请求复用 1 个持久任务；2 Body × 3 LOD 共 6 份网格均可下载且哈希正确，Head 不变 |
| 重建后真实 Worker | 重建后再为历史修订提交新的 scene 任务，同样 4 请求→1 任务、6 份网格验证通过，当前 Head 不变 |
| 重建后浏览器 | 两账户独立密码登录、共享参数面板和 3D 画布均可用，各建立 WebSocket，页面/控制台错误均为 0 |
| 真实 Provider | failed：`ProviderQuotaError`；Head 代次 0、无原生文件，没有假成功或未审核提交 |

跳过项不记为通过。CI 的 Provider 条件未开启，不能用绿色 CI 证明服务额度可用。云端验证使用真实 HTTPS、WebSocket、PostgreSQL、Temporal、MinIO 和 FreeCAD；确定性编辑测试没有调用模型，亦没有被标记为 LLM 验收。

主要真实任务：

- 协作修改：`f8c9d9de-f567-43e8-b9cc-45201bb0790b`。
- 浏览器修改：`50a9eb6b-5ff8-4923-9660-878a86debd8b`。
- 升级后场景：`bf5b59a7-f79e-4fea-a22a-f74b371979d6`。
- 重建后场景：`7231248c-c059-41a8-b2fc-6a2bc05a7104`。
- 额度失败：`cd6912f8-f10c-42fb-93cc-98821fcd0771`。

## 4. 几何与持久化证据

共享模型最终 Revision 为 `250774f3-db17-4329-914c-6f656f5e1480`，generation 为 3。把 HTTPS 下载的 FCStd 再交给网络隔离、只读根文件系统的 FreeCAD 1.1.3 容器独立打开测量：

| Body | 实体数 / 有效性 | 包围盒 mm | 体积 mm³ |
| --- | --- | --- | --- |
| BodyA | 1 / valid | 10 × 10 × 11 | 863.937979737193 |
| BodyB | 1 / valid | 10 × 10 × 11 | 863.937979737193 |

| 下载产物 | SHA-256 |
| --- | --- |
| FCStd | `6bb748369ff973d7fa0418a5d1a85e6a416a3a2068f70aaffed85d0227f55157` |
| 参数状态 | `63b582cbe64487463834a1b807533056548e97aaa746d7dcb6814e5796cfee00` |
| 网格 | `23bd31ed6bc1b99f106bfed87a532e4ab5bf8616d5b2ee627aefa66063c582c3` |

持久化演练先停 CAD 写入端和 Temporal，备份三个数据库及 MinIO/cad_data，再使用相同命名卷强制重建 PostgreSQL、MinIO、API、Worker、前端。5 个容器 ID 均已变化，Temporal 停止后重新启动。再次使用两个密码登录，权限、Revision/generation 和上述三个文件哈希完全一致。没有执行宿主机重启，也没有将该演练描述成整机灾备恢复。

备份位于仅 root 可读的 `/www/backups/cad-20260913`；首轮、`retry-1/` 和 `persistence/` 分开保存，没有覆盖迁移前备份。PG custom dump 已通过 `pg_restore --list`，归档列表与 SHA 已核对；本次没有执行从备份恢复数据库的灾备演练。

## 5. 失败记录与限制

| 过程问题 | 处理与结果 |
| --- | --- |
| Docker Hub MinIO 拉取失败 | 同版本 Quay 镜像验证并替换，CI 与腾讯云拉取通过 |
| 第一轮真实集成 BOM PermissionError | 修复临时文件所有权边界；最终真实集成 28 passed |
| PR #3 使用 `--auto` 后立即合并 | 仓库没有 required-check 门禁；完整检查尚未结束便合并。停止发布，修复失败后通过 PR #4 和 main 全部 CI 才部署；后续 PR 等检查通过再合并 |
| 初次云端启动拒绝裸 sandbox digest | 生产守卫正确拒绝；自动回退旧应用，保留已执行的兼容增量迁移和原备份。改成完整 `repository@sha256`，独立启动验证后第二次发布成功 |
| 私有测试脚本误把 lease DELETE 的 204 当成 200 | 按真实 API 合同修正 harness，复验通过；未改产品响应 |
| 模型调用额度不足 | 保留真实失败任务，没有替换 Provider 或伪造结果；补充服务额度后需重新执行生成、修改及修复/视觉专项 |

两个浏览器样本的首页导航约 15.5 s / 21.0 s，包含测试客户端网络与资源下载；不能作为服务器耗时或性能达标结论。主机仅 2 GB 内存，检查时磁盘剩余约 4.5 GB；未进行容量压测。当前单机 Docker socket 执行架构、每进程并发限制和共享资源不是公开多租户的隔离与容量保证。

真实 Fusion 桌面、物理设备和外部企业集成未在本次部署复验。完整产品验收仍需结合 [共同编辑实施报告](native-coediting-2026-09-13.md) 与 [验收整改记录](acceptance-remediation-2026-09-10.md)。

## 6. 证据与复验

可提交的结构化摘要位于 [evidence/tencent-2026-09-13/](evidence/tencent-2026-09-13/)。完整私有日志、浏览器 trace、截图和原生下载位于 `/private/tmp/cad-publish-20260913`；该临时目录不是长期归档，服务器备份和 Git 中摘要承担持久交接。凭据不包含在公开摘要中。

浏览器真实编辑入口使用仓库脚本 `backend/tests/e2e/native_packaged_browser.py`，测试账号配置通过私密文件路径传入。云端只运行非破坏性业务验证，未将会清空 fixture 表的 pytest 指向线上业务库。

运行配置、实际目录、配置预检和健康检查命令见 [腾讯云部署说明](../../deploy/tencent/README.md)。使用 `/ready` 验证依赖和 Worker，就绪不等于模型额度可用。
