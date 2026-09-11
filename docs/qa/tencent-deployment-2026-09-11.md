# 腾讯云部署验证报告

部署于 2026-09-10 开始，2026-09-11 完成验证。新版入口：**https://www.wordswave.ai**。
原 **https://wordswave.ai** 保留，原账号、项目和数据继续由旧站提供。

本次完成当前工作区版本的独立部署及下列真实调用链验证，不代表产品规划中的全部能力已经通过验收。

## 实际部署

- 主机：`129.226.74.100`，OpenCloudOS 9.6，AMD64，2 GB 内存、4 GB swap。
- 发布目录：`/www/releases/cad-20260910-5a208af-worktree/tencent`。
- 新版独立运行 API、Temporal Worker、前端、PostgreSQL、MinIO、Temporal。
  数据库迁移为 `0025_semantic_checkpoints`，使用独立网络和数据卷。
- 使用真实 Moonshot 配置及 FreeCAD 1.1.3，生产模式开启认证、关闭验证码回显。
  沙箱并发配置为 1，镜像以不可变摘要固定。
- 新域名证书已签发；原域名证书也已续期至 2026-12-09。
  修正 ACME 验证路径及续期后的 Nginx 重载命令，已有定时续期任务。
- S3 使用 HTTPS、私有桶和版本管理；数据库、Temporal、MinIO 控制台不暴露公网端口。
  临时排查使用的 8443 规则已撤销。
- 新旧数据库分离，**旧账号和项目未自动迁移到新版**。新版管理员沿用现有管理员密码。

源码为工作区快照，基于 Git HEAD `5a208af44ef7f164c0495d8c1f8fa15bbd06cbcc`，
并包含其未提交改动。1154 个源码文件的归档 SHA-256：
`ac8ecd2803a7cdfadb39d8001ab319c7ce4f4243379bc01cf8a49f82b1f8a296`。
详细记录见 [源码 manifest](../../.gstack/qa-reports/tencent-20260910/source-manifest.json)。

| 镜像 | 实际 OCI 摘要 |
|---|---|
| 后端及 Worker | `sha256:2d8f6dde9bdd93d10f6844a5a783cf9d02e3b4e0ba84eacb5b53b9cf40023092` |
| 原生计算沙箱 | `sha256:80ee8ac76543b48f170af9f86e59cc7d29c6ef812e24158de34af45b30ae0d09` |
| 前端 | `sha256:28fab21c469a68e341be34c359af95e59c98518c10072f720ba3e1e1eef14652` |

部署配置及重复执行方法见 [deploy/tencent/README.md](../../deploy/tencent/README.md)。

## 测试方法和结果

| 验证 | 结果 |
|---|---|
| 完整后端 pytest | 1374 通过、147 跳过；跳过项未算作通过 |
| 前端测试 / lint / 生产构建 | 98 通过，lint 和构建通过 |
| 后端依赖 | 实际 AMD64 镜像中 `pip check` 通过 |
| 源码与镜像一致性 | 后端 761、沙箱 467 个文件哈希一致；最终前端 9 个产物一致，无 AppleDouble 元数据文件 |
| 原生运行时探测 | FreeCAD 建模、保存重开修改、事务撤销、草图求解、实体校验、STEP/STL 导出和 BOM 通过；同时执行真实 Python CAD、DXF、渲染等运行时探测 |
| 真实 Agent 生成 | HTTPS/WebSocket → 需求与规划 → Temporal → FreeCAD → 验证 → S3 → 原生版本，真实任务完成 |
| HTTP 修改和权限 | 通孔直径 6→8 mm；提交前主版本不变；重复请求复用任务；过期版本写入 409；未授权读取 404、只读账号写入 403 |
| 外部浏览器修改 | 真实登录、草图直径 8→9 mm、局部预览、审查接受和提交、刷新恢复、WebGL 显示通过，页面运行错误为 0 |
| 独立几何测量 | 三个不可变版本的 FCStd 和 STEP 分别重新打开实测，孔数、位置、直径、贯通深度和体积全部一致 |
| FEA 浏览器与 HTTP | 真实 CalculiX 2.23，3633 节点，力平衡相对误差 `4.3823520266112495e-9`；显示、下载及哈希通过；无效请求、缺失实体明确失败且保持原版本 |
| CAM 严格浏览器用例 | 云端真实 Chromium 中通过：3 层、192 段、路径播放、NC 与证据下载、幂等性、短刀具失败、原版本保持；页面和控制台错误均为 0 |
| 容器重建后恢复 | API、Worker、前端和 MinIO 容器 ID 实际改变；公网重新登录、原生文件及历史 FEA/CAM 制品哈希均保持；模型仍为版本 3、9 mm 孔径 |
| 重建后新任务 | 新 Worker 实际执行新的 FEA 正反例，求解与制品校验通过；缺失实体仍明确失败 |

独立几何测量使用真实 FreeCADCmd 读取保存的文件，不依赖界面标签或返回的“成功”字段：

| 版本 | 平板尺寸 | 通孔直径 | 孔中心（相对左下角） | 深度 | FCStd/STEP 体积 |
|---|---|---|---|---|---|
| 生成 | 60×40×8 mm | 6 mm | (30,20) mm | 8 mm | 18973.80532894 mm³ |
| HTTP 修改 | 60×40×8 mm | 8 mm | (30,20) mm | 8 mm | 18797.87614034 mm³ |
| 浏览器提交 | 60×40×8 mm | 9 mm | (30,20) mm | 8 mm | 18691.06199012 mm³ |

最终原生版本：`cf3d0db4-6d14-4ba7-b9ef-8f3c94f9d980`。
FCStd SHA-256：`f2da04232d9907d4fc3c64273d89a16286cf783d8be7a033f736284f81a613f1`。

主要证据：

- [运行时探测](../../.gstack/qa-reports/tencent-20260910/runtime-probe.json)
- [镜像源码核验](../../.gstack/qa-reports/tencent-20260910/image-source-verification.json)
- [HTTP 与公网浏览器](../../.gstack/qa-reports/tencent-20260910/cloud-acceptance.json)
- [独立 FCStd/STEP 测量](../../.gstack/qa-reports/tencent-20260910/independent-geometry.json)
- [FEA 正反例](../../.gstack/qa-reports/tencent-20260910/cad-expansion-engineering-http.json)
- [CAM 严格浏览器](../../.gstack/qa-reports/tencent-20260910/cad-expansion-cam-browser.json)
- [重建后数据与浏览器恢复](../../.gstack/qa-reports/tencent-20260910/after-recreate-verification.json)
- [重建后新 Worker 的实际执行](../../.gstack/qa-reports/tencent-20260910/after-recreate-worker.json)

## 遇到的问题及处理

1. **迁移分叉。** 旧站是 `0013_guest_project_lifecycle` 分支，包含新版没有的游客模式和旧 Onshape 工作流。
   没有覆盖旧库、伪造迁移版本或删除旧站，改为独立部署。
2. **证书即将到期且续期重载目标错误。** 完成续期并改用正在运行的系统 Nginx。
3. **镜像解包空间不足。** 首次导出后的本地解包报磁盘不足；随后同一摘要成功解包，
   以完整运行时探测、源码哈希和实际 CAD/工程任务验证其可用性。保留原发布和回退镜像。
4. **前端打包包含 macOS 元数据。** 重新只打包普通产物、重新构建镜像，最终核验为 9 个真实应用文件。
5. **原始用例假设只生成 `PartDesign::Hole`。** 此次真实模型使用完整约束的圆草图与
   `PartDesign::Pocket`。原用例在类型查找处失败，未计为通过。
   新增部署验收按实际 `Diameter` 约束修改，并独立实测三个 FCStd/STEP 版本；未将类型名称当作几何正确性的证据。
6. **本机访问间歇性连接关闭。** 新旧站均观察到 TLS/连接关闭或超时；部分本机 CAM 严格用例因此失败。
   未找到服务器对应的 TLS 错误，网络根因尚未确认。外部核心浏览器、FEA 和重建恢复通过；
   原始严格 CAM 在云端真实 Chromium 复验通过。不能据此声称所有公网访问路径均稳定。

## 备份和未验证范围

旧库及旧容器/代理配置备份位于 `/www/backups/cad-20260910`，原发布目录保留。
新版曾在暂停写入和停止 MinIO 后备份应用数据库与对象文件，数据库备份目录清单可解析。
尚未执行灾难恢复演练；持久化复验是保留数据卷的实际容器重建，并非从备份恢复。

这是共享 2 GB 服务器上的低并发单机部署，未做高可用、容量或并发压力验收，
也未把 147 个条件跳过的 pytest 算作真实服务通过。Temporal 使用版本固定的单机 auto-setup 发行方式。

原方案中的硬件实机适配、企业插件/PLM，以及先前未完成的视觉修复预算耗尽负例等，
仍按 [独立整改报告](acceptance-remediation-2026-09-10.md) 的真实完成状态记录。
本次部署没有用 Mock、固定返回结果或“页面可打开”替代这些验收。
