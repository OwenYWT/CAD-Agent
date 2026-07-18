# Fusion 360 Connector API Contract

> Contract version: `1.0.0`
> Transport schema version: `1`
> 本文是 Web/Backend/Runtime/Add-in 共同遵守的稳定边界；生成的 JSON Schema 位于 `schemas/fusion360/`。

Cloud Agent exchange 使用独立 Agent contract `1.0.0`（线上字段仍为 `contract_version`），不改变核心 `CadAdapter` 1.0.0
接口；未来增加其他 CAD Adapter 时只需复用核心 Action/Result 生命周期。

## 1. Adapter interface

```python
from typing import Protocol

class CadAdapter(Protocol):
    def get_capabilities(self) -> CadCapabilities: ...
    async def get_context(self, query: ContextRequest) -> CadResult[ContextData]: ...
    async def execute(self, action: CadAction) -> CadResult[ActionData]: ...
    async def verify(self, specification: VerifyRequest) -> CadResult[VerificationData]: ...
```

`CadCapabilities`、`ContextRequest/ContextData`、discriminated `CadAction`、`ActionData`、`VerifyRequest/VerificationData`、`CadResult[T]` 和 Approval 都是 Pydantic 模型。另提供严格的 `JsonCadAdapter` 包装以兼容 JSON-compatible dict 四方法；它只负责 model_validate/model_dump。不得把 Fusion object、exception 或 filesystem absolute path 返回业务层。

`get_capabilities()` 是同步、cache/static-only，绝不进行 HTTP 或阻塞 event loop。`RemoteFusionAdapter.refresh_status()` 是实现专属的 async 方法，由 `/status` route 调用并更新 last-known capability status；其他 CAD Adapter 无需实现它。

## 2. Common request fields

```json
{
  "request_id": "d8990fae-6a51-4aac-a47f-b2f913efba8d",
  "idempotency_key": "user-workflow-step-42",
  "timeout_ms": 30000,
  "execution_mode": "preview",
  "approval_id": null
}
```

- `request_id`：UUID；调用者可提供，否则 Adapter 生成。
- `idempotency_key`：1–128 字符；同 key 不同 canonical payload 是冲突。
- `timeout_ms`：1000–300000；超时不表示 Fusion 线程被强制终止。
- `execution_mode`：`preview` 或 `execute`。只读 request 固定为 `execute`。
- `approval_id`：medium/high execute 必须提供，且与 canonical action intent hash 绑定。Intent hash 排除本节所有 request/transport 字段和 `execution_mode`，只包含 action/target/value/options。
- `connector_instance_id`：可选；多个在线 Connector 时必填。

Hash 规则：`action_intent_hash=SHA256(CAD-C14N-1({action,target,value/options}))`；`idempotency_payload_hash=SHA256(CAD-C14N-1({owner_id,phase,action_intent_hash}))`，phase 为 preview/execute。`CAD-C14N-1` 先使用 Pydantic 的 normalized model（包含 schema defaults、排除 request/transport 字段），递归把字符串正规化为 Unicode NFC，按 Unicode code point 排序 object key，以 UTF-8/无空白 JSON 编码；null/boolean 使用 JSON literal；integer 使用无前导零十进制；其他有限数字先按其十进制输入值转 Decimal，再输出无指数、无尾随零的普通十进制，`-0` 归一为 `0`。NaN/Infinity 拒绝。仓库中的 Unicode、key-order、integer/decimal 和 `-0` golden vectors 是跨 Backend/Runtime 兼容性依据；不得改用语言运行时默认 JSON/float 格式。唯一键 `(owner_id,idempotency_key,phase)`。Approval 一次性且 owner/connector/document/intent 绑定；同 execute phase replay 返回原结果，不能重做 mutation。

## 3. Context

```json
{
  "request_id": "uuid",
  "query": {
    "sections": [
      "application", "document", "design", "components", "occurrences",
      "selection", "parameters", "materials", "mass_properties",
      "sketches", "features", "timeline", "bodies", "assembly", "cloud"
    ],
    "max_depth": 8,
    "include_suppressed": false
  }
}
```

未知 section 拒绝；不以“返回所有内部对象”降级。响应的实体 ID 是 Fusion `entityToken` 或明确标记的 document/data ID。`timeline` 只表示 Parametric Design 的有界 `Design.timeline` 顺序摘要；它不等同于 Fusion UI Browser，Direct Design 可返回空列表并附 warning。

## 4. Action union

所有 Action 使用 `action` discriminator。首期 allowlist：

| Action | Target/Value 摘要 | Risk |
| --- | --- | --- |
| `cad.update_parameter` | document/component/parameter token + amount/unit 或 expression | medium |
| `cad.update_feature_parameter` | feature token + feature-owned parameter token + value | medium |
| `cad.create_sketch` | component + plane + line/circle/rectangle primitives | medium |
| `cad.create_extrude` | component + profile entity token + distance + operation | medium |
| `cad.create_hole` | component + sketch point token(s) + diameter + distance/through_all | medium |
| `cad.create_fillet` | component + edge tokens + radius | medium |
| `cad.create_chamfer` | component + edge tokens + distance | medium |
| `cad.update_entity_properties` | component/body/occurrence token + allowlisted name/part_number/description/material | medium |
| `cad.save_document` | document + version description | high |
| `cad.save_as` | document + DataFolder ID/token + name/description/tag | high |
| `cad.export` | document + format STEP/STL/DXF/F3D/PNG + safe filename/options | low |

示例：

```json
{
  "request_id": "2bd399ad-8c42-48c8-8c04-bc8326acd9ee",
  "idempotency_key": "resize-width-to-3mm-v1",
  "timeout_ms": 30000,
  "execution_mode": "preview",
  "approval_id": null,
  "action": "cad.update_parameter",
  "target": {
    "document_id": "urn:adsk.wipprod:dm.lineage:...",
    "component_id": "fusion-entity-token",
    "parameter_id": "fusion-parameter-token"
  },
  "value": {"amount": 3, "unit": "mm"}
}
```

`expression` 与 `amount/unit` 二选一。禁止以下字段：source code、module、function、command、argv、absolute output path、任意 property path。

### 4.1 Normative action shapes

以下字段定义是 normative；`backend/app/fusion360/contract.py` 是生成 JSON Schema 的唯一源码，`scripts/fusion360/generate_schema.py --check` 保证 tracked schema 与源码一致。Schema 文件携带 `$id=https://cad-agent.local/schemas/fusion360/<name>/1.0.0` 与 `x-contract-version=1.0.0`。代码和 schema 不一致时 CI 失败，不允许手改 schema。

Common types：

- `DocumentTarget = {document_id: str}`。
- `ComponentTarget = {document_id: str, component_id: str}`。
- `EntityTarget = {document_id, component_id?, entity_id, entity_kind}`，`entity_kind` 仅为 component/occurrence/body。
- `DimensionValue` 二选一：`{amount: finite number, unit: safe unit string}` 或 `{expression: 1..256 string}`。创建基础 geometry 的 unit 限制为 mm/cm/m/in/ft。
- `Point2 = {x: finite number, y: finite number}`；Sketch action 统一有 `unit`，点数按该 unit 转换。

Action variants：

| Action | Normative fields |
| --- | --- |
| `cad.update_parameter` | `target={document_id,component_id,parameter_id}`；`value=DimensionValue` |
| `cad.update_feature_parameter` | `target={document_id,component_id,feature_id,parameter_id}`；`value=DimensionValue`；Dispatcher 必须验证 parameter.createdBy=feature |
| `cad.create_sketch` | `target=ComponentTarget`；`plane` 为 `{kind:"origin", plane:"xy"|"xz"|"yz"}` 或 `{kind:"entity", entity_id}`；`unit`；`name?`；`primitives[]` discriminator：line(start,end)、circle(center,radius>0)、rectangle(corner1,corner2)，1..500 项 |
| `cad.create_extrude` | `target=ComponentTarget`；`profile_id`；`operation="new_body"|"new_component"|"join"|"cut"|"intersect"`；`distance=DimensionValue`；`direction="positive"|"negative"|"symmetric"`；join/cut/intersect 可带 `participant_body_ids[]` |
| `cad.create_hole` | `target=ComponentTarget`；`sketch_point_ids[1..100]`；`diameter=DimensionValue`；`extent` 为 distance(distance) 或 through_all(direction positive/negative) |
| `cad.create_fillet` | `target=ComponentTarget`；`edge_ids[1..500]`；`radius=DimensionValue`；`tangent_chain: bool=true` |
| `cad.create_chamfer` | `target=ComponentTarget`；`edge_ids[1..500]`；`distance=DimensionValue`；`tangent_chain: bool=true` |
| `cad.update_entity_properties` | `target=EntityTarget`；`properties` 至少一项，只允许 `name`、`part_number`、`description`、`material={library_id,material_id}`；各字段长度受限 |
| `cad.save_document` | `target=DocumentTarget`；`version_description: 0..1024`；只适用于 isSaved=true |
| `cad.save_as` | `target=DocumentTarget`；`data_folder_id`（由 context/cloud 返回并用 Fusion Data API 解析）；`name:1..255`、`description/tag`；只适用于首次保存或显式另存 |
| `cad.export` | `target=DocumentTarget`；`format="step"|"stl"|"dxf"|"f3d"|"png"`；`filename` safe relative basename 且扩展名必须匹配格式（STEP 接受 `.step/.stp`）；STEP/F3D 可选 component_id；STL 可选 body/component target、`mesh_refinement=low|medium|high`、`binary`；DXF **必须** `sketch_id`；PNG 有 `width/height` 64..4096；不允许 absolute path |

每个 action 还含 common request fields。Pydantic model 使用 `extra="forbid"`；format-inapplicable options 导致 validation error。

`ActionData` 也是 discriminated union：`preview` 返回 planned_changes/verification_plan；`mutation` 返回 snapshot_id/created_or_updated_entity_ids/compensation；`save` 返回 local_save_accepted/cloud_version_processing/before_version/after_version；`export` 返回 format 和 artifact IDs。`ContextData` 的 application/document/design/component/occurrence/selection/parameter/material/mass/sketch/feature/body/cloud sections 都有显式模型，不使用无界 `dict[str, Any]`。仅 `vendor_extensions` 允许 namespaced JSON 值。

## 5. Verify specification

```json
{
  "request_id": "uuid",
  "timeout_ms": 30000,
  "specification": {
    "document_id": "doc-id",
    "baseline_request_id": "optional-prior-mutation-request-id",
    "source_request_id": "optional-save-or-export-request-id",
    "checks": [
      {
        "check": "parameter_equals",
        "target_id": "parameter-token",
        "expected": {"amount": 3, "unit": "mm"},
        "tolerance": 0.000001
      },
      {"check": "no_new_feature_errors"},
      {"check": "entity_resolves", "target_id": "parameter-token"},
      {"check": "artifact_valid", "artifact_id": "artifact-id"}
    ]
  }
}
```

首期 verify checks：`parameter_equals`、`entity_resolves`、`no_feature_errors`、`no_new_feature_errors`、`local_save_accepted`、`cloud_version_complete`、`artifact_valid`。Standalone `no_new_feature_errors` 必须有 `baseline_request_id` 或 check-level `snapshot_id`；save/artifact checks 必须有 `source_request_id`。首期 Runtime 不自动清理 snapshot/result/artifact；运维清理后引用丢失返回 `BASELINE_NOT_FOUND`/`SOURCE_RESULT_NOT_FOUND`。Action-internal verify 可直接引用当前 Dispatcher 内存 snapshot，不需要公网 reference。未保存文档只能 `save_as`；`save_document` 返回 `DOCUMENT_UNSAVED`。保存 result 分别报告 local accepted 与 cloud processing pending/complete。

## 6. Result

```json
{
  "request_id": "uuid",
  "status": "success",
  "action": "cad.update_parameter",
  "data": {},
  "changes": [
    {
      "target_id": "parameter-token",
      "path": "parameter.expression",
      "kind": "updated",
      "before": "2 mm",
      "after": "3 mm"
    }
  ],
  "warnings": [],
  "verification": {
    "passed": true,
    "checks": [],
    "compute_completed": true,
    "new_feature_errors": []
  },
  "artifacts": [],
  "approval": null,
  "error": null
}
```

`status`：`queued`、`running`、`success`、`failed`、`cancelled`、`timeout`、`approval_required`、`offline`、`indeterminate`。`indeterminate` 表示 mutation 可能生效但结果提交/验证链未完整完成，系统禁止自动重放并要求 verify/reconcile。

Medium/high Action Preview 的 status 为 `approval_required`，`changes` 仍为空，`data.planned_changes` 描述预期变化；`approval` 为 `{approval_id, intent_hash, risk, expires_at, connector_instance_id, document_id}`。Low-risk export 可选 Preview 返回普通 `success` 且不生成 approval；其 execute 可直接调用。任何 Preview 都必须无写副作用。

Artifact：

```json
{
  "artifact_id": "uuid",
  "kind": "step",
  "filename": "part.step",
  "media_type": "model/step",
  "size_bytes": 12345,
  "sha256": "hex",
  "download_url": "/api/cad/fusion360/artifacts/<request>/<filename>"
}
```

这是 **Web/Public CadArtifact**；Backend 在收到 IPC result 后注入 authenticated proxy URL。Add-in → Runtime 的 `LocalArtifact` 只有 artifact_id/kind/filename/media_type/size_bytes/sha256/relative_path，不含 `download_url`。绝对路径只存在于经 Connector role 鉴权、lease 约束且带 artifact-root fingerprint 的 executor context 和本地日志，不进入 Backend/Web response。

## 7. Error

```json
{
  "code": "DOCUMENT_READ_ONLY",
  "message": "The active Fusion document is read-only",
  "category": "read_only",
  "retryable": false,
  "details": {}
}
```

稳定 code：

- `CONNECTOR_OFFLINE`, `NO_ACTIVE_DOCUMENT`, `NO_ACTIVE_DESIGN`
- `DOCUMENT_MISMATCH`, `DOCUMENT_UNSAVED`, `DOCUMENT_READ_ONLY`; `CONFIGURATION_READ_ONLY` is reserved for a future released configuration API and is not emitted by the current production adapter
- `TARGET_NOT_FOUND`, `TARGET_AMBIGUOUS`, `PARAMETER_NOT_FOUND`, `FEATURE_NOT_FOUND`
- `INVALID_ACTION`, `UNSUPPORTED_ACTION`, `UNSUPPORTED_FUSION_VERSION`
- `APPROVAL_REQUIRED`, `APPROVAL_INVALID`, `APPROVAL_EXPIRED`
- `IDEMPOTENCY_CONFLICT`, `REQUEST_CANCELLED`, `REQUEST_TIMEOUT`
- `FUSION_API_ERROR`, `COMPUTE_FAILED`, `VERIFICATION_FAILED`
- `EXPORT_FAILED`, `ARTIFACT_INVALID`, `PATH_NOT_ALLOWED`
- `CLOUD_NOT_CONFIGURED`, `CLOUD_AUTH_REQUIRED`, `CLOUD_RATE_LIMITED`
- `PROTOCOL_MISMATCH`（validation，non-retryable，HTTP 426）
- `STALE_LEASE`（conflict，non-retryable for that worker，HTTP 409）
- `CONNECTOR_SELECTION_REQUIRED`（conflict，retryable after caller selection，HTTP 409）
- `BASELINE_NOT_FOUND`, `SOURCE_RESULT_NOT_FOUND`（not_found，non-retryable，HTTP 404）
- `ARTIFACT_ROOT_MISMATCH`（artifact，non-retryable，HTTP 409）
- `CREDENTIAL_ROLE_MISMATCH`（auth，non-retryable，HTTP 403）
- `APPROVAL_ALREADY_USED`（approval/conflict，non-retryable，HTTP 409）
- `OWNER_MISMATCH`（not_found，non-retryable，HTTP 404；避免资源枚举）
- `QUEUE_FULL`（rate_limit，retryable，HTTP 429）、`ARTIFACT_QUOTA_EXCEEDED`（artifact，retryable after cleanup，HTTP 507）
- `RATE_LIMITED`（Web-facing per-credential rate_limit，retryable，HTTP 429）

## 8. Web/Backend endpoints

```text
GET  /api/cad/fusion360/status
GET  /api/cad/fusion360/capabilities
POST /api/cad/fusion360/context
POST /api/cad/fusion360/actions
POST /api/cad/fusion360/verify
GET  /api/cad/fusion360/requests/{request_id}
POST /api/cad/fusion360/requests/{request_id}/cancel
GET  /api/cad/fusion360/artifacts/{request_id}/{filename}

GET  /api/cad/fusion360/cloud/status
POST /api/cad/fusion360/cloud/oauth/start
GET  /api/cad/fusion360/cloud/oauth/callback
DELETE /api/cad/fusion360/cloud/oauth/token
GET  /api/cad/fusion360/cloud/hubs
GET  /api/cad/fusion360/cloud/hubs/{hub_id}/projects
GET  /api/cad/fusion360/cloud/projects/{project_id}/top-folders?hub_id={hub_id}
GET  /api/cad/fusion360/cloud/projects/{project_id}/folders/{folder_id}/contents
GET  /api/cad/fusion360/cloud/projects/{project_id}/items/{item_id}/versions
GET  /api/cad/fusion360/cloud/projects/{project_id}/versions/{version_id}
```

所有 Web-facing endpoints 复用项目认证/限流。Runtime connector endpoints 不暴露到公网。

Capabilities/status 同时返回 `runtime_online`、`connector_online` 和 `fusion_running`。`fusion_running=true` 只由 Fusion 内 Add-in 心跳提供正向证据；Connector 离线时为 `null`（未知），不得把 Add-in 未加载误报成 Fusion 进程已退出。

## 9. Compatibility rules

- 新增 optional response 字段是 backwards-compatible；删除/重命名字段或改变语义需要 contract major version。
- Add-in 注册时报告 `protocol_versions`；Runtime 选择共同版本，否则拒绝连接。
- Action schema 由 Backend 生成到 `schemas/fusion360/`；Add-in 在运行时校验 transport/version/required fields，Runtime 负责完整 Pydantic validation。
- Unknown action 永远失败，不做动态反射或字符串方法分派。

## 10. Connector IPC envelope

Register request/response：

```json
{
  "connector_instance_id": "stable-uuid",
  "protocol_versions": [1],
  "fusion_version": "2.x",
  "addin_version": "1.0.0",
  "platform": "macos",
  "user_name": "redacted-or-display-name",
  "artifact_root_fingerprint": "sha256"
}
```

```json
{
  "protocol_version": 1,
  "heartbeat_interval_ms": 5000,
  "heartbeat_ttl_ms": 15000,
  "lease_duration_ms": 30000
}
```

Task lease：

```json
{
  "protocol_version": 1,
  "request_id": "uuid",
  "operation": "execute",
  "intent_hash": "sha256",
  "lease_id": "uuid",
  "attempt": 1,
  "leased_until": 1780000000,
  "deadline": 1780000030,
  "payload": {},
  "execution_context": {
    "artifact_dir": "/executor-only/absolute/path",
    "artifact_root_fingerprint": "sha256"
  }
}
```

`started`/`result` 必须携带相同 `connector_instance_id/request_id/lease_id/attempt/intent_hash`。Runtime 只用 compare-and-set 接受当前 lease；stale lease 返回 409 `STALE_LEASE`。Heartbeat 固定 5s、TTL 15s；断线退避 1–30s+jitter。`control` 返回 `{cancel_requested, deadline, lease_valid}`，worker 把它传播为 Dispatcher 可读取的 cancel token。

修改任务在 durable journal 写入 started 后不自动重租。只有 read/context/verify/export 可在 policy 允许时重试；started mutation 失联进入 `indeterminate`。这是一项明确的 at-most-once mutation 安全保证，不虚假承诺分布式 exactly-once。

Journal 更新使用同目录临时文件、flush/fsync、atomic replace 和目录 fsync（平台支持时）。读取到截断/损坏记录、atomic replace 失败、或 request/intent/lease/attempt 与当前 task 不匹配时必须 fail closed：报告结构化 reconciliation/indeterminate 状态并拒绝执行或重放可能已经完成的 mutation；不得静默丢弃 journal 后继续。

Artifact absolute path 只能出现在 executor-only envelope；用户 Action 与公网结果不含绝对路径，IPC `LocalArtifact` 也不含 URL。首期 Backend/Runtime/Add-in 同主机，Backend artifact proxy 是 Web 唯一下载入口。

Deadline 语义：pre-start 超时最终为 `timeout`；mutation accepted as started 后触发 deadline 则最终为 `indeterminate`，迟到验证只作为 reconciliation evidence。read-only 迟到结果不覆盖 timeout。

Backend/Connector credentials 是不同 role 且不得相同。Backend enqueue payload 包含 opaque owner_id；所有 request/status/cancel/approval/snapshot/artifact 操作按 owner 隔离，跨 owner 返回 404。Connector 只能读被租给自己的 task，不能调用 Backend-facing endpoints。

资源限制：普通 request body ≤1 MiB，带 Connector role 鉴权的 result ≤16 MiB；primitives/edge IDs/verify checks ≤500/500/100；queue 默认 ≤1000；每 Connector 同时一个 mutation；artifact 单文件 ≤512 MiB、每 owner ≤2 GiB。队列配置可降低，不能越过代码硬上限。首期无自动 retention；运维清理后旧 source/baseline reference 会返回 not-found。

## 11. Direct HTTPS Cloud Agent exchange

默认 Fusion Palette 路径不使用上述 loopback lease envelope。它使用同一核心 `CadAction` /
`CadResult`，另加 Agent exchange models（生成 schema 位于 `schemas/fusion360/`）：

```text
GET  /api/cad/fusion360/agent/capabilities
POST /api/cad/fusion360/agent/heartbeat
GET  /api/cad/fusion360/agent/connectors/{connector_instance_id}/status
POST /api/cad/fusion360/agent/plan
POST /api/cad/fusion360/agent/results
PUT  /api/cad/fusion360/agent/artifacts/{request_id}/{filename}
GET  /api/cad/fusion360/agent/artifacts/{request_id}/{filename}
```

所有 endpoint 都使用项目现有 `Authorization: Bearer ...` / API credential 鉴权并经过限流。
`plan`/`results` 是 JSON；artifact 是 raw bytes，必须带 `Content-Type`、`X-Artifact-Size` 和
`X-Artifact-SHA256`。统一错误 envelope 为
`{"error":{"code":"...","message":"...","retryable":false}}`。

`GET capabilities` 返回 `contract_versions:["1.0.0"]`、核心 allowlisted `actions`、
`max_actions_per_plan=1`、turn/artifact 字节上限，以及 F3D 独立授权标志。客户端必须先确认有
共同的精确 contract version；不存在共同版本时 fail closed。

Heartbeat 每 5 秒由 HTTPS worker 发送 `{contract_version,connector_instance_id,fusion_version,
addin_version,platform,capabilities,sent_at}`；服务端 TTL 为 15 秒。status 在线时
`connector_online=true,fusion_running=true`；TTL 过期或从未见过该 Connector 时
`connector_online=false,fusion_running=null`，因为服务端不能区分 Fusion 已退出和 Add-in 被禁用。
status 和 artifact download 都按 authenticated owner 隔离。

### 11.1 Agent turn

```json
{
  "contract_version": "1.0.0",
  "request_id": "uuid",
  "connector_instance_id": "uuid",
  "prompt": "把 Width 改为 30 mm",
  "context_fingerprint": "ctx-c14n-1:<sha256>",
  "context": {
    "document": {"document_id": "doc-id", "name": "Part", "document_type": "FusionDesignDocumentType", "is_saved": true, "is_modified": false, "is_read_only": false},
    "parameters": [],
    "truncated": false
  },
  "capabilities": {
    "adapter": "fusion360",
    "contract_version": "1.0.0",
    "protocol_versions": [1],
    "available": true,
    "runtime_online": false,
    "connector_online": true,
    "fusion_running": true,
    "actions": ["cad.update_parameter", "cad.create_extrude"],
    "context_sections": ["document", "parameters", "features", "timeline"],
    "limitations": []
  },
  "export_artifact_upload_consent": false,
  "f3d_upload_authorized": false
}
```

HTTP bearer credential 解析为 authenticated subject，服务端持久化/approval 绑定使用 subject，
不信任 body 自报 owner。`prompt` 和 context 有明确大小/数量上限；`context.truncated=true` 时
Agent 只能返回 clarification，不能生成 Action。`f3d_upload_authorized=true` 必须同时有普通
artifact upload consent。

`context_fingerprint` 是服务端可重算的 `ctx-c14n-1` 投影：document 保存/修改/只读状态、
cloud version/project/folder/read-only、selection/feature 的 ID/kind/component/health、parameter 的
ID/component/createdBy/expression，以及完整 bounded timeline fields；它不包含 prompt、用户名、
display name、mass 或 vendor extension。列表按稳定 ID/index 排序后用 `CAD-C14N-1` 编码。
本地核心 `get_context` 可以返回 Fusion `application.user_name`；Direct Agent 出站 turn 必须将其
清空为 null，避免把不参与规划的桌面用户身份发送到云端。

### 11.2 Agent plan

```json
{
  "contract_version": "1.0.0",
  "request_id": "uuid",
  "proposal_id": "uuid",
  "connector_instance_id": "uuid",
  "status": "proposed",
  "context_fingerprint": "ctx-c14n-1:<sha256>",
  "action": {
    "request_id": "与 turn 相同的 uuid",
    "idempotency_key": null,
    "timeout_ms": 30000,
    "action": "cad.update_parameter",
    "execution_mode": "preview",
    "approval_id": null,
    "connector_instance_id": "uuid",
    "target": {"document_id": "doc-id", "component_id": "component-token", "parameter_id": "parameter-token"},
    "value": {"amount": 30, "unit": "mm"}
  },
  "risk": "medium",
  "question": null,
  "reason": "将 Width 调整为 30 mm",
  "expires_at": "2026-07-16T12:05:00Z"
}
```

`status=proposed` 时恰好一个 action；`needs_clarification|no_action` 时 action 必须为 null。
服务端覆盖模型生成的 request、connector、execution_mode、approval、idempotency 字段，
重新经 `CAD_ACTION_ADAPTER` 验证并按本地 policy 计算 risk/expiry。所有实体 ID 必须来自请求
context 且满足 document/component/kind ownership。模型返回 code/script/module/function/command/
attribute path 等任一动态执行形状都会拒绝，不存在静态 Action fallback。

### 11.3 Native preview and approval

Plan response 不是 Preview。Add-in 必须在 Fusion 主线程用同一 Dispatcher 执行
`execution_mode=preview`，得到无写入的 `PreviewData` 后才可显示 approval。Approval nonce
绑定 authenticated subject、connector、document、`CAD-C14N-1` intent、context fingerprint、
proposal/preview/request ID、risk 和 expiry；执行入口原子单次消费。save/saveAs 另需 Fusion
原生确认。执行前重新采集 fingerprint；变化返回 `STALE_CONTEXT`。

### 11.4 Execution report

```json
{
  "contract_version": "1.0.0",
  "report_id": "uuid",
  "request_id": "uuid",
  "proposal_id": "uuid",
  "connector_instance_id": "uuid",
  "context_fingerprint": "ctx-c14n-1:<sha256>",
  "action_intent_hash": "64-char-hex",
  "result": {
    "request_id": "uuid",
    "status": "success",
    "action": "cad.update_parameter",
    "data": {"kind": "mutation", "snapshot_id": "uuid", "created_or_updated_entity_ids": ["parameter-token"], "compensation": null},
    "changes": [],
    "warnings": [],
    "verification": {"passed": true, "checks": [], "compute_completed": true, "new_feature_errors": []},
    "artifacts": [],
    "approval": null,
    "error": null
  },
  "completed_at": "2026-07-16T12:01:00Z"
}
```

服务端验证 request/proposal/connector/fingerprint/intent hash/result action 全部与持久化 plan 一致。
同 `report_id` + 相同 canonical payload 返回 `status=duplicate`；changed payload 返回 409。
任意层级 `_snapshot`、`_local_artifacts` 或其他 `_...` private key 都会在 HTTP 边界拒绝；审计库
只存绑定 metadata，不持久化完整 result。plan/report 可有限重试；已 started mutation 不得因
网络错误重新执行，只有 completed report 可从 journal 原样重发。

### 11.5 Artifact upload

默认 report 只含公开 artifact metadata。上传前必须先接受同 request 的成功 `cad.export`
execution report；turn 必须有 `export_artifact_upload_consent=true`，filename 必须等于 planned
export 的 safe basename。F3D 还要求 turn 的 `f3d_upload_authorized=true`，且 Add-in 本地另一个
一次性 F3D nonce 已消费。上传请求示意：

```text
PUT /api/cad/fusion360/agent/artifacts/<request_id>/fixture.step
Content-Type: model/step
X-Artifact-Size: 12345
X-Artifact-SHA256: <64-char-hex>

<raw file bytes>
```

服务端流式计算 size/SHA-256、检查 planned format 的文件 signature，以 owner/request/filename
隔离存储；receipt 返回受鉴权的 `download_url`，Web 使用相同 credential GET。重复相同文件
返回 duplicate，changed bytes/claim 冲突。缺少授权、path traversal、format/hash/size 不符或
超限均 fail closed。不存在由 Palette/Agent 自行签发的 upload claim。

### 11.6 Version negotiation

Add-in 先读取 `contract_versions`，只选择双方都明确支持的精确版本。未知 capability/action 不
降级执行。新增 optional response 字段可后向兼容，删除字段、放宽审批或改变执行语义必须提升
major version。
