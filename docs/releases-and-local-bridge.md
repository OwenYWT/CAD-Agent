# 版本发布与本地文件交付

面向项目所有者、管理员和部署维护者。以下行为以 2026-09-09 的实际代码及原生内核、HTTP、PostgreSQL、浏览器和本地客户端验收为准。

## 发布流程

在云文档的“版本发布与 BOM”中填写唯一发布名称，可选择最多 8 项同一文档、同一修订、同一 FCStd 哈希且已成功完成的有限元或外轮廓加工任务。所有者或管理员可以发布；编辑者可以下载，审阅者可以查看发布详情及 BOM。

提交时固定原生检查点、特征标注版本和工程工件引用。事务同时保存命名发布、工程任务、WorkflowRun 和 Temporal dispatch。计算复用已有隔离执行、输入校验、执行租约和产物封存流程，不创建 CAD 修改操作，也不移动文档 head。重复请求返回同一发布；同名的新请求、过期版本、不同内容复用请求 ID、旧修订的工程证据都会被拒绝。

原生 FreeCAD 实际生成：

- `design.FCStd`：原检查点的逐字节副本。
- `design.step` / `design.stl`：全部最终实体和实例的 STEP / STL。
- `bom.json` / `bom.csv`：通过真实 `Assembly::BomObject` 读取的部件名称、数量、实例和定义关系；包含隐藏的最终实体。CSV 对可能被表格软件解释为公式的名称转义，JSON 保留原名称。
- `bom-source.FCStd`：实际生成 BOM 的派生原生文档，含表格与几何；原设计不被改变。
- 选定工程任务的原始报告、场数据、求解/加工证据包和 NC 程序。
- `manifest.json`：修订、源 FCStd 哈希、文件大小与 SHA-256、工程证据、特征标注及单位。

归档为 `cad-engineering-release.v1`，BOM 为 `cad-release-bom.v1`。发布包覆盖 1–1000 个有效实体部件/实例，解压后内容预算 128 MiB。当前导出包含几何及应用内标注，不宣称带有其他 CAD 厂商的原生特征历史、供应商零件号、成本、物料审批或 ERP 主数据。

## 本地 Bridge

网页“本地 Bridge 交付”可下载真实独立客户端 `cad_local_bridge.py`。客户端只依赖 Python 3.11+ 标准库，当前支持 macOS/Linux。仓库入口为 `python scripts/local_bridge.py`，网页下载与仓库入口使用同一份实现。

管理员生成一次性配对码，有效期 10 分钟。客户端在终端中读取配对码，将单独的项目范围凭据写入权限 `600` 的配置文件。服务端仅保存随机密钥的 SHA-256。配对码用后清除；撤销连接或撤销配对管理员的项目授权会拒绝之后的客户端请求。

客户端不开放本地 HTTP 端口，主动通过 HTTPS 连接云服务；本机回环地址允许 HTTP。它不会跟随重定向携带凭据。客户端的连接凭据不能用于普通用户 API，只能领取该连接的发布文件任务。所有配对、交付和回执表均启用强制租户 RLS。

```bash
# 从仓库运行；输出目录须已存在且可写，配对码在交互提示中输入。
python3 scripts/local_bridge.py pair --server https://cad.example.com \
  --directory /approved/cad-output --config ~/.cad-bridge.json
python3 scripts/local_bridge.py run --config ~/.cad-bridge.json
```

网页下载的客户端使用同样参数。省略 `--directory` 时会询问实际绝对路径。`run --once` 处理最多一项任务并退出，适合受控调度；通常使用持续运行模式，客户端每 10 秒轮询。

用户选择已完成发布和已配对连接后，服务端保存不可变归档来源并排队。客户端领取 5 分钟租约，处理期间每 40 秒续租。它验证归档及清单哈希、文件集合、大小预算、路径、类型和原生源哈希；拒绝重复项、符号链接和目录穿越。文件写入权限为 `600`，通过临时目录、`fsync` 和原子重命名落盘，再逐一重新读取并校验。

目录布局为 `输出目录/项目 UUID/文档 UUID/发布 UUID/`，不把用户填写的发布名称解释为路径。重复交付会重新验证已有文件，保留本地修改并报告冲突。只有服务端验证全部文件的回执后才显示“文件已送达”。文件校验或本地写入失败会保存失败记录，修复后可以新建交付；旧失败记录保持不变。网络或确认响应中断时，通过过期租约及新租约 token 恢复，旧客户端无法确认新租约；最多领取 5 次。

## 能力边界

此 Bridge 已接入本地及挂载目录的文件交付，实际验收使用 macOS 本地目录。企业 SMB/NFS/PLM 目标需要部署方提供可访问的挂载点或确定厂商接口后验证。它不运行任意插件或 shell 命令，也不自动启动 NC 加工。没有实际 CNC、USB/串口设备型号、协议、机床设置和授权时，不宣称实机执行完成。

现有 Onshape 和 Fusion 连接保持独立。2026-09-09 的本轮环境没有配置 Onshape 凭据，也未提供真实 Fusion 桌面、PLM 或物理机床；这些外部环境不能通过本地文件交付或协议测试替代验收。

## 接口与部署

- `POST/GET /api/documents/{id}/releases`：创建和列举命名发布。
- `GET /api/documents/{id}/releases/{release_id}`：读取经过校验的发布报告及授权下载引用。
- `POST /api/documents/{id}/bridges/pair`、`GET /api/documents/{id}/bridges`、`DELETE /api/documents/{id}/bridges/{bridge_id}`：项目连接管理。
- `POST /api/documents/{id}/releases/{release_id}/deliveries`：不可变发布文件交付。
- `/api/local-bridge/client`、`/pair`、`/poll` 及 `/deliveries/{id}/archive|renew|ack|fail`：客户端下载与独立设备凭据协议。

升级需执行 `alembic upgrade head`（至少 `0024_local_bridge`）并重建 API、Worker、前端和沙箱镜像。客户端包含在 API 镜像内，不需要额外挂载脚本。发布复用 Temporal V1 队列；Bridge 队列保存在 PostgreSQL，由本地客户端领取，不依赖浏览器保持打开。

## 已执行验收

- `freecad_release_acceptance.py`：真实多实体/实例、3 个 STEP 实体、原生 BOM、CSV 公式名称、原模型不变、每个 ZIP 文件哈希；使用最终镜像、不覆盖内核源码。
- `cloud_release_acceptance.py` / `cloud_release_browser.py`：HTTP/浏览器 → Temporal → 原生 BOM/导出 → S3 → 下载，真实 CAE/CAM 证据选择、权限、冲突及幂等。
- `cloud_release_database.py`：同源输入/输出、单次真实执行、dispatch、无 CAD 修改、不可变发布与交付回执、租户 RLS。
- `cloud_bridge_acceptance.py`：真实 CLI 配对及 14 文件交付，305 秒真实租约等待、客户端重新运行、服务端重启、已有文件不重写、旧回执拒绝、凭据撤销。
- `cloud_bridge_browser.py`：下载独立客户端、网页配对、配对码清除、真实磁盘写入、网页回执与撤销；零页面及控制台错误。
- `cloud_bridge_failure.py`：实际符号链接目录导致明确失败、目标无写入，修复后新交付成功且旧失败记录不变。
- `test_local_bridge.py`：实际 ZIP 和文件系统的路径/完整性/重放/配置权限/进程锁测试，以及发布、能力声明输入合同。
- 既有真实 FreeCAD/CadQuery/装配 BOM 回归 5 项通过；本模块全后端 1329 项、前端 94 项通过。
