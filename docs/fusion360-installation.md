# Fusion 360 Connector 安装、启动、调试与卸载

默认拓扑是 Fusion Add-in / Palette 直接出站 HTTPS 连接 Cloud Agent；不需要把 Local Runtime
暴露到网络，也不要求每台工作站常驻 Backend Python。`local_runtime` 仍可用于离线/诊断。

## 1. 前置条件

- Autodesk Fusion 当前受支持版本；截至 2026-07-17 官方要求页为 Windows 11 23H2+ 或
  macOS 14+，部署前再次核对最新要求。
- 有权安装当前用户 Add-in，不需要管理员权限。
- Cloud Agent HTTPS URL 和由 CAD Agent Backend/组织 credential provider 签发的 scoped
  bearer token。不要复用或尝试读取 Fusion 登录 token。
- 要读取 Hub/Project/Version 时，另外配置 Backend APS OAuth；该 token 不进入 Add-in。

截至 2026-07-17 的交付审计没有真实 Fusion Desktop 执行证据。自动测试验证了 schema、
HTTPS worker、Palette/controller 和窄 Fusion facade，但不能代替下文 Windows/macOS 真实
Fusion 验收。

## 2. 安装

macOS：

```bash
scripts/fusion360/install.sh --dry-run
scripts/fusion360/install.sh
```

Windows PowerShell：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\fusion360\install.ps1 -WhatIf
.\scripts\fusion360\install.ps1
```

当前 Autodesk 用户级目录：

- macOS：`~/Library/Application Support/Autodesk/Autodesk Fusion/API/AddIns`
- Windows：`%APPDATA%\Autodesk\Autodesk Fusion\API\AddIns`

新安装只写当前目录；卸载脚本也清理可能存在的历史 `Autodesk Fusion 360` 路径副本。Add-in state 默认位于
`~/.cad-agent/fusion360`（Windows 为 `%USERPROFILE%\.cad-agent\fusion360`）。升级保留
connector UUID、token/config、artifact staging 和 mutation journal。

## 3. 配置 Direct Cloud Agent

复制 [`fusion_addin/config.example.json`](../fusion_addin/config.example.json) 到 state 目录的
`connector.json`，填写非敏感配置：

```json
{
  "mode": "cloud_agent",
  "agent_url": "https://cad-agent.example.com",
  "agent_token_file": "/absolute/user-private/path/agent.token",
  "connector_instance_id": "stable-uuid",
  "artifact_root": "/absolute/user-private/path/artifacts",
  "journal_path": "/absolute/user-private/path/execution-journal.json",
  "request_timeout_s": 30,
  "queue_size": 16,
  "approval_ttl_s": 600
}
```

`agent_url` 的公网地址必须 HTTPS；仅 `127.0.0.1`/`::1` 允许开发 HTTP。URL 不得带用户名、
密码、query 或 fragment，redirect 会被拒绝。Bearer token 可以改由
`CAD_AGENT_FUSION_TOKEN` 注入，但生产优先组织 credential provider 或 owner-only token file。

macOS：

```bash
chmod 700 ~/.cad-agent ~/.cad-agent/fusion360
chmod 600 ~/.cad-agent/fusion360/connector.json ~/.cad-agent/fusion360/agent.token
```

Windows 安装脚本使用 `icacls` 移除敏感文件继承并只授权当前账号。不要把真实 URL/token、
绝对用户路径或 APS secret 提交到仓库。

## 4. 在 Fusion 启动

1. 打开 **Utilities → Scripts and Add-Ins → Add-Ins**。
2. 选择 `CADAgentFusionConnector`，启用 **Run on Startup** 并运行。
3. 打开 **CAD Agent** Palette。
4. Cloud Agent status endpoint 在 heartbeat 15 秒 TTL 内显示 Connector/Fusion 在线；Palette
   显示当前交互状态。提交请求后应依次显示 context、Agent proposal、原生 Preview Diff、
   Approval、执行/重建/验证结果。

Palette 是本地 UI，不直接访问网络或 Fusion API。它通过 Qt Web Browser 的异步
`adsk.fusionSendData` 与 Add-in 通信；token 不会进入 HTML/JavaScript。

## 5. 调试

- Fusion 日志查看 Add-in lifecycle/Dispatcher 的本地 traceback；公网 `CadError` 不带 traceback。
- 检查 `connector.json` 模式、HTTPS hostname、系统 CA/企业代理和 token 权限。
- 不要为“排错”关闭 TLS 校验、允许 redirect、删除 started-only journal 或把 Agent Action
  改成动态 Python。
- 如果 journal 有 `started` 但无 `completed`，状态必须是 `indeterminate`；先读取 context 和
  verify/reconcile，禁止重放 mutation。

Cloud Agent Backend 健康检查和鉴权方式由部署提供。一个最小 plan contract probe（使用测试
context，不要上传真实设计）见 [`fusion360-web-example.md`](fusion360-web-example.md)。

## 6. Optional local_runtime compatibility

只有离线/企业代理/诊断需要本地 Runtime：

```bash
scripts/fusion360/install.sh --with-local-runtime
scripts/fusion360/start-runtime.sh
```

```powershell
.\scripts\fusion360\install.ps1 -WithLocalRuntime
.\scripts\fusion360\start-runtime.ps1
```

把 `connector.json` 的 `mode` 改为 `local_runtime`，配置 loopback `runtime_url` 和独立
connector secret file。Runtime 只监听 `127.0.0.1:8765`，Backend/Connector role secret
必须不同；绝不能把它作为公网 Cloud Agent 暴露。

## 7. 升级和卸载

```bash
scripts/fusion360/upgrade.sh --dry-run
scripts/fusion360/upgrade.sh
scripts/fusion360/uninstall.sh --dry-run
scripts/fusion360/uninstall.sh
```

PowerShell 使用对应 `upgrade.ps1` / `uninstall.ps1` 与 `-WhatIf`。卸载只移除本 Add-in，默认
保留 state/token/journal/artifact 以便 reconciliation。确认没有 indeterminate mutation 后再由
用户显式清理 state。

## 8. Windows/macOS 真实 Fusion 验收门槛

在独立测试 Hub/Project 和可恢复 fixture 上执行，每项保存 request/response、Fusion before/
after 截图、entity token、Diff、verification、artifact hash、OS 和 Fusion version：

- [ ] Palette 本地资源/CSP 生效；prompt/Agent 文本不能注入 HTML，token 不出现在开发工具。
- [ ] 无活动文档、非 Design、未保存、只读、目标缺失/歧义返回稳定错误。
- [ ] Context 返回 Application/Document/Design、Root/Components/Occurrences、selection、
      parameters/units/material/mass、Sketch/Feature/Body/assembly/cloud 和 parametric Timeline；
      Timeline 明确不是 UI Browser 1:1 镜像。
- [ ] 真实 HTTPS：context → Agent → 单个 typed proposal；恶意/未知字段、动态代码和 stale
      context 被 Add-in 二次校验拒绝。
- [ ] Proposal 先执行 native no-write preview；拒绝无写入；approval nonce 不能重复或跨
      credential/connector/document/context/action 使用。
- [ ] Parameter/Feature parameter、Sketch、Extrude、Hole、Fillet、Chamfer、名称/材料分别执行；
      `computeAll` 完成、无新增 Feature Error、目标重定位、assertion 和 Diff 都有证据。
- [ ] save/saveAs 在 Palette approval 后出现 Fusion 原生确认；未保存文档只允许 saveAs；
      local accepted 与 cloud pending/complete 分开报告。
- [ ] STEP/STL/DXF/PNG 导出验证路径/格式/size/hash 后才上传；F3D 缺少独立 full-model
      authorization 时拒绝上传。
- [ ] Agent 断连/429/timeout、Fusion 重启、completed report 重发和 started-only indeterminate
      均符合状态机；mutation 从不自动重放。
- [ ] workspace 切换后 Palette 重建；停止时先 join worker 再注销 handler/CustomEvent；没有
      late response 访问失效 Palette。
- [ ] install/upgrade/uninstall 在当前 Windows/macOS 各执行一轮，权限和 state 保留符合文档。

未完成两平台真实链路前，正确交付状态是“代码与模拟验证完成，真实 Fusion 验收待执行”，
不得声明 production-ready。
