# Autodesk Fusion 360 Connector 官方调研

> 调研日期：2026-07-16；外部链接与系统要求复核：2026-07-17
> 事实来源：仅使用 Autodesk 官方帮助、Autodesk Platform Services（APS）官方文档与 Autodesk 官方公告。
> 验证环境：截至 2026-07-17 的交付审计未在真实 Fusion 进程中执行，因此本文不会声称 live API 行为已通过；可确定的 API 事实来自下列官方页面，真实环境验证步骤见 `docs/fusion360-installation.md`。

## 1. 结论摘要

1. **实时、交互式、当前文档相关的能力必须由 Fusion Desktop Add-in 执行。** `Application`、活动文档、UI 选择集、当前编辑目标、未保存修改、Feature 健康状态和视口截图都属于桌面会话状态，APS Data Management 无法代替这些能力。
2. **Fusion Desktop API 正式面向 Python 与 C++；Fusion Automation 使用 TypeScript 脚本。** 桌面 Connector 首期选择 Python Add-in，原因是跨 Windows/macOS 可以共享源码且无本地编译步骤；C++ 二进制必须分别在 Windows/Visual Studio 与 macOS/Xcode 构建。官方：[Python 特有事项](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/PythonSpecific_UM.htm)、[C++ 特有事项](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/CPPSpecific_UM.htm)、[Automation API 概览](https://aps.autodesk.com/developer/overview/automation-api)。
3. **任何工作线程都不得调用 Fusion API。** 出站 HTTPS 在 worker thread 中执行；worker 只把 JSON 放入内存队列并调用 `Application.fireCustomEvent`。CustomEvent 回调由 Fusion 在主线程空闲时执行，所有 `adsk` 调用集中到该回调中的 Dispatcher。官方还明确把“worker 访问 Web 服务、完成后通知主线程更新命令”列为正确用例。官方：[Working in a Separate Thread](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Threading_UM.htm)、[`Application.fireCustomEvent`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Application_fireCustomEvent.htm)。
4. **云端 Automation 现在确实支持 Fusion。** Fusion Automation API 于 2025-05 正式商用，可在云端执行 Fusion TypeScript 脚本、修改/创建几何和制造数据、导入导出文件。它适合无 UI 的批处理，不适合读取用户桌面当前选择、未保存状态或活动视口。官方：[Automation API 概览](https://aps.autodesk.com/developer/overview/automation-api)、[Fusion Automation 正式商用公告](https://aps.autodesk.com/blog/design-automation-api-fusion-now-generally-available)。
5. **云文件元数据存在可贯通的 ID。** `DataFile.id` 是 APS Data Management Item ID，`DataFile.versionId` 是版本 ID；因此桌面上下文可返回这些 ID，Web/Backend 在用户授权后再用 APS 查询 Hub/Project/Item/Version。官方：[`DataFile`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/DataFile.htm)。
6. **实体 token 可持久化定位，但不能直接比较字符串。** 同一实体在不同时刻可能返回不同 token；必须用 `Design.findEntityByToken` 解析后比较实体。拓扑变更后一个 token 也可能解析到多个实体。官方：[`UserParameter.entityToken`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/UserParameter_entityToken.htm)、[`Design.findEntityByToken`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Design_findEntityByToken.htm)。

## 2. Fusion Desktop API

### 2.1 对象模型与职责

官方对象模型从 `Application` 开始：Document 可以同时包含 Design、CAM 等 Product；Design 有唯一 Root Component；Component 拥有 sketches、features、bodies、construction geometry，并可被一个或多个 Occurrence 引用。官方：[Getting Started with Fusion's API](https://help.autodesk.com/view/fusion360/ENU/?guid=GUID-D93DF10F-4209-4073-A2A0-4FA8788C8709)、[Documents, Products, Components, Occurrences, and Proxies](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ComponentsProxies_UM.htm)。

| 对象 | Connector 使用方式 | 官方事实/限制 |
| --- | --- | --- |
| `Application` | 版本、当前用户、在线状态、activeDocument/activeProduct/activeViewport、事件和 CustomEvent | 顶层对象；活动文档/产品/视口在无文档时为 null；提供文档、数据、材料库和事件。[官方](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Application.htm) |
| `Document` | 名称、creationId、isSaved、isModified、isUpToDate、DataFile、保存/另存 | 首次保存必须 `saveAs`；后续版本使用 `save`；保存不能在 Command 相关事件事务中执行。[对象](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Document.htm)、[`save`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Document_save.htm)、[`saveAs`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Document_saveAs.htm) |
| `Design` | designType、rootComponent、allComponents、allParameters、units、materials、exportManager、computeAll、findEntityByToken | `designType` 在 Parametric→Direct 时会删除 timeline，不能作为一般操作暴露；文档写权限另由已发布的 DataFile read-only 状态判断。[官方](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Design.htm) |
| `Component` | 组件属性、bodies、sketches、features、occurrences、质量属性 | Component 是几何/特征/参数的定义，可被多个 Occurrence 引用；Root 的 `allOccurrences` 是扁平视图。[官方](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Component.htm) |
| `Occurrence` | 装配树、fullPathName、transform、grounded、external reference | Occurrence 是 Component 的装配实例；外部引用通过 DocumentReference 获取版本信息。[官方](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Occurrence.htm) |
| `Sketch` | 平面、曲线/点/约束/Profile 摘要，创建线、圆、矩形 | Sketch 由 Component 拥有；`Sketches.add` 接收平面或平面面；Profile 在闭合轮廓后计算。[对象](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Sketch.htm)、[`Sketches.add`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Sketches_add.htm) |
| `Feature` | 名称、type、suppressed、healthState、错误/警告、token | 修改后必须检查 `healthState`；只有 Warning/Error 状态时 `errorOrWarningMessage` 才非空。[healthState](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Feature_healthState.htm)、[message](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Feature_errorOrWarningMessage.htm) |
| `Parameter` | name、expression、numeric value、unit、createdBy；文本参数保留 expression | 优先设置带单位的 `expression`；裸数字按文档默认单位解释，内部数值采用数据库单位。当前生产实现不依赖仍标记 Preview 的 `valueType`。[官方](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Parameter_expression.htm) |
| `Selection` | 读取 `UserInterface.activeSelections` 中每个 Selection 的 entity/point | Selection 只是选中对象包装；远程任务不得触发交互式 select 对话。[官方](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Selection.htm) |
| `BRepBody` / `PhysicalProperties` | body 摘要、solid/visible、面积、体积、质量、密度、质心 | `area` 为 cm²、`volume` 为 cm³；默认 physicalProperties 使用 LowCalculationAccuracy，更高精度需调用 getPhysicalProperties。[官方](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/BRepBody.htm) |
| `ExportManager` | STEP、STL、Fusion Archive、Sketch DXF | 先创建格式特定 options，再调用 `execute`；返回 boolean 仍需验证文件存在、大小和格式。[官方](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ExportManager.htm) |

“特征树”不能被描述为 1:1 读取 Fusion UI Browser 节点。官方公开对象应组合为：Occurrence/Component 装配层级、每个 Component 的 Feature/Sketch/Body，以及 Parametric Design 的 Timeline/TimelineObject 顺序；Direct Design 没有同等时间线语义。官方：[`Timeline`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Timeline.htm)、[`TimelineObject`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/TimelineObject.htm)。

### 2.2 受控建模 API

- **Sketch**：首期只支持明确平面/平面实体和类型化 primitive（line、circle、two-point rectangle），不接受任意 Python。官方矩形签名：[`SketchLines.addTwoPointRectangle`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/SketchLines_addTwoPointRectangle.htm)。
- **Extrude**：使用闭合 Sketch Profile 与 `ValueInput.createByString("… mm")` 创建 `NewBody`/`Join`/`Cut`；官方简单样例展示 `extrudeFeatures.addSimple`，完整输入展示 `createInput`/extent。[官方样例](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/SimpleExtrusionSample_Sample.htm)。
- **Hole**：`HoleFeatures.createSimpleInput` 定义直径，位置由 SketchPoint(s) 定义，extent 使用 distance 或 through-all；官方样例给出 `setPositionBySketchPoints` 与 `setDistanceExtent`。[官方样例](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/HoleFeatureSample_Sample.htm)。
- **Fillet**：使用 `FilletFeatureInput.edgeSetInputs.addConstantRadiusEdgeSet`，输入必须是显式 BRepEdge token 集合。[官方样例](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/FilletFeatureSample_Sample.htm)。
- **Chamfer**：使用非 retired 的 `ChamferFeatures.createInput2` 和 `chamferEdgeSets.addEqualDistanceChamferEdgeSet`。[官方样例](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/EqualDistanceChamferFeature_Sample.htm)。
- **参数更新**：UserParameter 与 ModelParameter 都可通过 `expression` 更新。Feature 参数更新只允许 context 中明确返回、且 `createdBy` 与目标 Feature 相符的 ModelParameter；不通过猜测 `d1/d2` 或时间线索引定位。
- **重新计算与验证**：`Design.computeAll()` 等价于 Compute All，但其 `true` 只说明计算过程完成，**不说明所有 Timeline/Feature 均健康**。执行后仍必须比较新增 Feature 错误、参数表达式/值、实体 token 可解析性，并对导出文件做磁盘级验证。官方：[`Design.computeAll`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Design_computeAll.htm)、[`FeatureHealthStates`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/FeatureHealthStates.htm)。

### 2.3 预览与导出

- STEP/STL/F3D 通过 `Design.exportManager`。截至本次核对，`ExportManager.createDXFSketchExportOptions`（2025-01 引入）仍被官方标记为 **Preview**，且页面明确要求分发程序不要依赖 preview capability；旧 `Sketch.saveAsDXF` 已于 2025-07 retired，但官方说明它由新 API 替代且仍公开。首期默认使用 legacy 方法并在能力/限制中显式报告，不默认启用不允许分发的 Preview API；待 Autodesk 将新方法转正后切换。[DXF options 官方](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ExportManager_createDXFSketchExportOptions.htm)；[legacy 方法官方](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Sketch_saveAsDXF.htm)
- Web 预览首选当前 Viewport PNG：`Viewport.saveAsImageFile`；可选同时导出 STL/STEP 作为可交互 Web Viewer 输入。官方：[`Viewport.saveAsImageFile`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Viewport_saveAsImageFile.htm)。
- API 返回 `true` 只代表调用报告成功；Connector 还要验证路径位于受控 artifact root、文件存在、非空、扩展名/头部合理并计算 SHA-256。

## 3. Add-in 生命周期、事件、线程和 UI

### 3.1 生命周期与安装

Fusion 调用 Add-in 的 `run(context)` 启动，调用 `stop(context)` 卸载。Add-in 通常在整个 Fusion 会话中常驻，`stop` 必须停止 worker、注销 CustomEvent、移除 UI 控件并释放 handler 引用。脚本则在 `run` 完成后结束。官方：[Creating a Script or Add-In](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/WritingDebugging_UM.htm)、[Python Add-in Template](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/PythonTemplate_UM.htm)。

默认 Add-in 搜索目录：

- Windows：当前官方安装说明使用 `%appdata%\Autodesk\Autodesk Fusion\API\AddIns`；新安装写当前路径，卸载器也清理历史 `Autodesk Fusion 360` 路径中本 Connector 的副本。
- macOS：当前官方安装说明使用 `~/Library/Application Support/Autodesk/Autodesk Fusion/API/AddIns`；新安装写当前路径，卸载器也清理历史路径副本。
- 其他目录可通过 Scripts and Add-Ins 对话框链接；`.manifest` 是 JSON，`id` 必须是唯一 GUID，`description` 是支持语言代码的 JSON 对象，`supportedOS` 可设为 `windows|mac`，`runOnStartup` 控制自启动。

### 3.2 线程硬边界

Python 在 Fusion 进程和主线程内执行，长循环会冻结 UI。官方明确要求 worker thread **不得调用任何 Fusion API，包括 messageBox**。正确流向是：

```text
HTTPS worker（stdlib only；loopback compatibility 可用 HTTP）
  -> 入队纯 JSON
  -> Application.fireCustomEvent(event_id)
  -> Fusion idle 时在主线程调用 CustomEventHandler
  -> Dispatcher -> Fusion Adapter -> adsk API
  -> 纯 JSON 结果入队
  -> HTTPS worker 回传 Cloud Agent（或显式 local-runtime 兼容端点）
```

若修改会产生 Undo 项，调用仍必须等待 Fusion 主线程可安全处理 CustomEvent；保存操作不能在 Command 相关事件事务中执行，因此保存与几何修改是两个显式 action，不做隐式“修改即保存”。当前实现不使用 text command 强制切换活动命令，Fusion 拒绝当前状态下的操作时返回结构化错误。

### 3.3 事件与 UI

- Application 提供 documentOpened/Closed/Activated/Saving/Saved、dataFileComplete、onlineStatusChanged、startupCompleted 等事件；它们可用于后续缓存失效和状态推送，但当前首期上下文每次实时读取，不注册这些非必要监听，也不用事件直接执行远程修改。
- Palette 是 Fusion 内的嵌入式浏览器。官方明确指出 Palette HTML/JavaScript **不能调用 Fusion API**；HTML 必须用 `adsk.fusionSendData` 触发 `Palette.incomingFromHTML`，Add-in 用 `Palette.sendInfoToHTML` 回传字符串（通常为 JSON）。新 Qt Web Browser 下 `fusionSendData` 返回 Promise，因此 Palette 实现必须按异步结果处理。官方：[Using Palettes and Browser Command Inputs](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Palettes_UM.htm)、[`Palette.incomingFromHTML`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Palette_incomingFromHTML.htm)、[`Palette.sendInfoToHTML`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Palette_sendInfoToHTML.htm)。
- `UserInterface.palettes.add` 可加载 Add-in 同目录的本地 HTML 或 Web URL；本 Connector 选择本地 HTML，并通过 CSP 禁止 Palette 自己联网，让 token/TLS/重试只存在于 Python worker。当前 `Palettes.add` 官方页说明 Fusion 已始终使用 Qt Web Browser，`useNewWebBrowser` 参数不再产生差异；代码不得依赖旧 CEF 的同步行为。官方：[`Palettes.add`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Palettes_add.htm)。
- Palette 在 workspace 切换时会被 Fusion 删除，旧对象 `isValid=false`；Add-in 必须按需重建，并在 `stop` 删除/释放 handler。Palette 只显示上下文、Agent proposal、Preview Diff、Approval 和结果，远程 Agent 不能借 Palette 注入代码。
- Fusion 不持久化 API 产生的 UI 定制，Add-in 每次启动重建，停止时移除。官方：[UI Customization](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/UserInterface_UM.htm)、[Events](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Events_UM.htm)。

## 4. Fusion 文件、Hub、Project、版本与云数据

### 4.1 桌面 `DataFile` 能力

已保存 Document 的 `dataFile` 可返回：

- Item ID：`id`；Version ID：`versionId`；`versionNumber` / `latestVersionNumber`；
- `parentFolder` / `parentProject`；Fusion Web URL；
- `isComplete`（云处理是否完成）、`isReadOnly`、`isInUse`、child/parent reference 和 out-of-date 状态；
- `thumbnail` 是异步的 256×256 PNG；原生 F3D 不能用 `DataFile.download`/`dataObject` 直接下载，官方只允许这些方法读取非 Fusion 数据。

保存调用返回后云处理仍可能继续，因此“保存成功”和“云版本处理完成”是两个状态；可监听 `Application.dataFileComplete` 或检查 `DataFile.isComplete`。

### 4.2 APS Data Management

Data Management API 支持 Fusion Team，并按 Hub → Project → Folder → Item → Version 导航；Project Service 负责 Hub/Project，Data Service 负责 Folder/Item/Version 元数据，OSS 承载二进制。官方：[Data Management API](https://aps.autodesk.com/developer/overview/data-management-api)、[Hubs Browser 官方教程](https://get-started.aps.autodesk.com/tutorials/hubs-browser/viewer/)。

Connector 的 Cloud Connector 首期只做用户明确授权后的只读导航：

```text
GET /project/v1/hubs
GET /project/v1/hubs/{hub_id}/projects
GET /project/v1/hubs/{hub_id}/projects/{project_id}/topFolders
GET /data/v1/projects/{project_id}/folders/{folder_id}/contents
GET /data/v1/projects/{project_id}/items/{item_id}/versions
GET /data/v1/projects/{project_id}/versions/{version_id}
```

路径中的 URN 必须 URL 编码；不同 APS API 对 readable、URL-encoded、base64url URN 的要求不同，不能混用。官方：[Data Management IDs](https://aps.autodesk.com/blog/ids-data-management-api)。

### 4.3 OAuth

- Web Backend 使用 OAuth 2.0 v2 三方授权（authorization code），读取默认仅申请 `data:read`；需要写云数据时必须开启独立配置并重新请求最小写 scope。
- Desktop/SPA 无法保密 client secret 时使用 PKCE。服务端可以保管 secret，但仍使用 state、防重放、精确 redirect URI。
- token endpoint 为 `https://developer.api.autodesk.com/authentication/v2/token`；refresh token 也通过该 endpoint。
- Token 不写日志、不返回前端、不放 Add-in 配置；在 Backend 使用部署提供的 Fernet key 加密后落盘。

官方：[Authentication API](https://aps.autodesk.com/developer/overview/authentication-api)、[OAuth v2/PKCE 迁移](https://aps.autodesk.com/blog/migration-guide-oauth2-v1-v2)、[Desktop PKCE](https://aps.autodesk.com/blog/getting-token-pkce-desktop-app-0)。

## 5. Fusion Automation API

Fusion Automation API 的工作单元是 Engine + AppBundle + Activity + WorkItem。Fusion 当前使用 TypeScript 脚本；它可以创建/修改 3D geometry、制造模型/操作、分析数据、导入导出多种文件。官方：[Automation Developer Guide](https://aps.autodesk.com/en/docs/design-automation/v3/developers_guide/overview/)。

能力边界：

| 任务 | Desktop Add-in | Fusion Automation |
| --- | --- | --- |
| 当前活动文档、选择、编辑目标、视口 | 唯一正确来源 | 不可访问用户桌面会话 |
| 未保存修改 | 可访问 | 不可访问，除非先显式上传/提供输入 |
| 交互式低延迟修改 | 首选 | WorkItem 开销不合适 |
| 大批量、无人值守、横向扩展 | 本机资源受限 | 首选 |
| 云原生文件处理 | 需桌面在线 | 首选，但需 OAuth、Activity/AppBundle、成本和配额 |
| 任意代码风险 | Connector 禁止 Agent 代码 | 同样只部署审计过的固定 AppBundle，不接受 Agent 任意脚本 |

Automation 自 2025-03-31 起要求 `code:all` scope；端点有配额，`GET workitems/:id` 轮询曾调整为 150 RPM，官方推荐服务端 onComplete callback 或客户端 WebSocket，并要求对 429 读取 `Retry-After`。Activity/AppBundle 的版本/alias 也有服务限制。官方：[scope 公告](https://aps.autodesk.com/blog/design-automation-api-enforcing-oauth-scope-deadline-extension-march-31-2025)、[WorkItem 限流公告](https://aps.autodesk.com/blog/new-design-automation-rate-limit-get-workitemid)、[Rate Limiting](https://aps.autodesk.com/blog/rate-limiting)。

首期不把 Automation 放到默认执行路径；能力声明为 `available=false/configured=false`，直到部署方配置 APS App、固定 AppBundle/Activity、计费与数据上传审批。

## 6. 本地 API 与云 API 的职责边界

| 能力 | 本地 Fusion API | APS Data Management / MDM | Fusion Automation |
| --- | --- | --- | --- |
| 当前 UI、选择、active document | 是 | 否 | 否 |
| 参数/Feature/Sketch 的实时读取与改动 | 是 | MDM 只暴露其当前支持的数据模型，不等同完整桌面对象模型 | 是，基于 WorkItem 输入文件 |
| 未保存/dirty/read-only 状态 | 是 | 只见云版本 | 否 |
| Hub/Project/Folder/Item/Version 导航 | DataFile 给当前文档局部元数据 | 是，权威云导航 | 通过输入/输出 URL 使用 |
| 批量 headless 修改 | 不适合 | 不执行 CAD | 是 |
| Web 交互预览 | 本地导出/截图 | 可配合 Model Derivative/Viewer | 输出文件后再展示 |

默认策略：Cloud Agent 只接收当前桌面的**结构化上下文摘要**并返回类型化 `CadAction`；动作仍由本地 Fusion API 完成。APS Data Management 只做授权后的云导航，Automation 只做显式的 headless 批处理。Cloud Agent 本身不是 APS，也不能替代 Desktop API。完整 CAD 文件默认不上传。

## 7. Windows/macOS、权限、限流和许可证

### 7.1 平台

截至 2026-07-17 核对的 Autodesk 官方系统要求页：Windows 最低为 Windows 11 23H2 build 22631；macOS 最低为 macOS 14 Sonoma。Windows ARM64 通过 emulation 尚未完成 Fusion 认证；Fusion 在 Apple Silicon 原生运行，但部分命令仍可能需要 Rosetta 2。官方：[System requirements for Autodesk Fusion](https://www.autodesk.com/support/technical/article/caas/sfdcarticles/sfdcarticles/System-requirements-for-Autodesk-Fusion-360.html)。

Connector 平台策略：

- Python Add-in 源码相同，路径/文件权限/启动脚本分平台；默认 Cloud Agent 模式只发起出站 HTTPS，显式 local-runtime 兼容模式仍只允许 `127.0.0.1`/`::1`。
- macOS 配置/token 文件权限设为 `0600`，目录 `0700`；Windows Add-in 使用当前用户 AppData，并移除敏感文件继承 ACL、仅授权当前账号；不写 Program Files，不要求管理员权限。首期使用受控 token file/provider 接口；Keychain/Credential Manager device enrollment 是生产组织部署的后续 credential provider，不把密钥硬编码进 Add-in。
- Add-in 对自有 Cloud Agent 使用系统 trust store 校验的 HTTPS，禁止 redirect、URL 内嵌凭据和 Palette JavaScript 网络访问。APS OAuth token 仍只在 Backend，不进入 Add-in。
- 安装脚本不修改 Fusion 安装目录，只复制用户级 AddIns 目录；卸载只删除本 Connector 的 Add-in 目录并保留配置、凭据、journal 与 artifact state。

### 7.2 许可证与只读状态

- `Data.personalUseLimits` 暴露 Personal Use license 的 editable file count 与上限；`DataFile.isReadOnly` 可因 Personal Use 可编辑文件额度、他人编辑或配置设计等原因为 true。Connector 在任何修改前依赖已发布的 `DataFile.isReadOnly` 信号；不调用仍属 Preview 的配置检查 API。官方：[`PersonalUseLimits`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/PersonalUseLimits.htm)、[`DataFile`](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/DataFile.htm)。
- 某些 Manufacture API 能力依赖 Fusion Manufacturing Extension；例如官方 DataFile 页面引用的 Hole Recognition 样例明确要求该扩展。Connector 首期的 Design workspace 基础 Hole Feature 不宣称包含 Manufacture Extension 能力。
- APS 从 2025-12-08 起采用 Free/Paid 两层；Automation、Model Derivative 等 rated APIs 有月度免费额度，超额需 Paid，具体数值可能更新，部署时必须查询当前 rate sheet，不能把历史价格硬编码。官方：[APS business model evolution](https://aps.autodesk.com/blog/aps-business-model-evolution)。

### 7.3 限流

- Cloud Agent/Connector 自己限制并发、队列长度、上下文大小和每请求 timeout；Add-in 每次只执行一个 mutation，不把 Fusion 主线程当并发执行器。显式 local-runtime 模式保持相同上限。
- APS 收到 429 必须读取 `Retry-After` 并指数退避；列表端点使用分页/缓存，优先 Webhook/callback 而非高频轮询。
- 不在代码写死从旧公告取得的所有端点 RPM；官方服务配额和 developer hub 计划可能变化，运行时以响应头和当前 APS 文档为准。

## 8. 对实现的硬约束

1. Agent 只能提交 Pydantic 定义的 discriminated action，不能提交 source code、module、callable、命令行或任意属性路径。
2. Fusion 专属 `adsk` 引用只出现在 Add-in 的 `fusion_api.py`/入口文件中；业务 Backend 只依赖 `CadAdapter`。
3. 网络 worker 只处理 JSON/HTTP；主线程 Dispatcher 是唯一 Fusion API 入口。
4. 每次修改记录目标语义快照；中高风险任务先 preview，execute 必须携带与规范 hash 绑定的短期 approval ID。
5. Action 后必须 `computeAll`、检查新增 Feature Error、重定位目标并生成结构化 before/after Diff；导出必须验证实际文件。
6. 真实环境不可用时，只允许名为 test/simulator 的替身用于协议和故障测试，生产 Adapter 不回退到 simulator。

## 9. 官方来源索引

- [Fusion API User Manual](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/UserManualIndex_UM.htm)
- [Fusion API Object Model / Getting Started](https://help.autodesk.com/view/fusion360/ENU/?guid=GUID-D93DF10F-4209-4073-A2A0-4FA8788C8709)
- [Application](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Application.htm)
- [Document](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Document.htm)
- [Design](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Design.htm)
- [Component](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Component.htm)
- [Occurrence](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Occurrence.htm)
- [Feature](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Feature.htm)
- [Sketch](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Sketch.htm)
- [Parameter expression](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Parameter_expression.htm)
- [ExportManager](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ExportManager.htm)
- [Threading](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Threading_UM.htm)
- [Creating Scripts and Add-ins](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/WritingDebugging_UM.htm)
- [Using Palettes and Browser Command Inputs](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Palettes_UM.htm)
- [Palette.incomingFromHTML](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/Palette_incomingFromHTML.htm)
- [DataFile](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/DataFile.htm)
- [APS Authentication](https://aps.autodesk.com/developer/overview/authentication-api)
- [APS Data Management](https://aps.autodesk.com/developer/overview/data-management-api)
- [APS Automation](https://aps.autodesk.com/developer/overview/automation-api)
- [Fusion Automation GA](https://aps.autodesk.com/blog/design-automation-api-fusion-now-generally-available)
- [Install an Add-in and Script in Fusion](https://www.autodesk.com/support/technical/article/caas/sfdcarticles/sfdcarticles/How-to-install-an-ADD-IN-and-Script-in-Fusion-360.html)
- [Fusion system requirements](https://www.autodesk.com/support/technical/article/caas/sfdcarticles/sfdcarticles/System-requirements-for-Autodesk-Fusion-360.html)
