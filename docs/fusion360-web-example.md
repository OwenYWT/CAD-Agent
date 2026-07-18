# Fusion 360 Web / Backend 调用示例

默认生产链路由 Fusion Palette 发起：它读取当前桌面上下文并调用 Cloud Agent；普通浏览器不持有
Fusion 对象、entity token 或 Add-in bearer token。Backend 提供下面的 HTTPS contract，Add-in 已
内置客户端。Local Runtime/Core `CadAdapter` REST 仍作为可替换兼容入口。

完整字段以 [`fusion360-api-contract.md`](fusion360-api-contract.md) 和
[`schemas/fusion360/`](../schemas/fusion360/) 为准。

## 1. Cloud Agent 能力协商

```bash
curl https://cad-agent.example.com/api/cad/fusion360/agent/capabilities \
  -H "Authorization: Bearer $CAD_AGENT_FUSION_TOKEN"
```

客户端必须确认返回的 `contract_versions` 包含 `1.0.0`、`max_actions_per_plan` 为 1，并且需要
的 action 出现在 allowlist。不要在不兼容时猜测降级格式。

Web 可按稳定 Connector UUID 读取在线状态：

```bash
curl https://cad-agent.example.com/api/cad/fusion360/agent/connectors/11111111-1111-4111-8111-111111111111/status \
  -H "Authorization: Bearer $CAD_AGENT_FUSION_TOKEN"
```

只有在线 heartbeat 才返回 `fusion_running=true`；过期/未知返回 `null`，不会把 Add-in 离线
误报成 Fusion 进程已退出。

## 2. 构造并提交 Agent turn

以下 Backend 例子直接复用生产 Pydantic model 来生成可重算的 fingerprint；实际 Add-in 在
Fusion 主线程采集同一 `ContextData`，网络 worker 只发送生成后的 JSON。

```python
import uuid
import httpx

from app.fusion360.agent_contract import AgentTurnRequest, context_fingerprint
from app.fusion360.contract import CadCapabilities, ContextData

context = ContextData.model_validate({
    "application": {"name": "Autodesk Fusion", "version": "2.x"},
    "document": {
        "document_id": "doc-id",
        "name": "Fixture",
        "document_type": "FusionDesignDocumentType",
        "is_saved": True,
        "is_modified": False,
        "is_read_only": False,
    },
    "design": {
        "design_type": "parametric",
        "root_component_id": "component-token",
        "units": "mm",
    },
    "components": [{
        "id": "component-token", "name": "Root", "kind": "component"
    }],
    "selection": [{
        "id": "parameter-token", "name": "Width", "kind": "parameter",
        "component_id": "component-token"
    }],
    "parameters": [{
        "id": "parameter-token", "name": "Width", "expression": "20 mm",
        "value": 2.0, "unit": "cm", "is_user_parameter": True,
        "component_id": "component-token", "created_by_id": None
    }],
    "features": [],
    "timeline": [],
    "truncated": False,
})

turn = AgentTurnRequest(
    request_id=uuid.uuid4(),
    connector_instance_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
    prompt="把 Width 改为 30 mm",
    context=context,
    context_fingerprint=context_fingerprint(context),
    capabilities=CadCapabilities(
        available=True,
        connector_online=True,
        fusion_running=True,
        actions=["cad.update_parameter"],
        context_sections=["document", "components", "selection", "parameters"],
    ),
    export_artifact_upload_consent=False,
    f3d_upload_authorized=False,
)

with httpx.Client(
    base_url="https://cad-agent.example.com",
    headers={"Authorization": f"Bearer {TOKEN}"},
    timeout=30,
) as client:
    plan = client.post(
        "/api/cad/fusion360/agent/plan",
        json=turn.model_dump(mode="json"),
    ).raise_for_status().json()
```

同 `request_id` 和同一 canonical turn 可安全重试并得到原 plan；同 ID 不同 turn 返回 409。
`proposed` 只表示计划，不能直接当作执行成功。Add-in 会再次验证 action/semantic IDs/risk，重新
读取 context，执行 native no-write preview，再向用户展示一次性 approval nonce。

## 3. 回报真实执行结果

用户批准后，Add-in 通过 Dispatcher/Fusion API 修改、`computeAll`、Feature health delta、目标
重定位、action assertion 和 Diff 验证，然后提交：

```python
from datetime import datetime, timezone

from app.fusion360.agent_contract import AgentExecutionReport
from app.fusion360.contract import CAD_ACTION_ADAPTER
from app.fusion360.policy import action_intent_hash

report = AgentExecutionReport(
    request_id=plan["request_id"],
    proposal_id=plan["proposal_id"],
    connector_instance_id=plan["connector_instance_id"],
    context_fingerprint=plan["context_fingerprint"],
    action_intent_hash=action_intent_hash(CAD_ACTION_ADAPTER.validate_python(plan["action"])),
    result=fusion_public_result,  # AgentExecutionReport validates the CadResult union.
    completed_at=datetime.now(timezone.utc),
)

receipt = client.post(
    "/api/cad/fusion360/agent/results",
    json=report.model_dump(mode="json"),
).raise_for_status().json()
```

生产 Connector 不使用 `exec`、`eval`、动态 import、subprocess 或任意方法名 dispatch。公网
result 不得包含任何 `_...` private field、本地绝对路径或 executor snapshot。

## 4. 经授权上传导出文件

只有 turn 已声明 upload consent、plan action 是精确的 `cad.export`、成功 export report 已被
接受，且 filename 与 plan 一致时才可上传。F3D 还需 turn 与 Palette 的独立双重批准。

```python
from pathlib import Path
import hashlib

path = Path("fixture.step")
size = path.stat().st_size
sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

with path.open("rb") as stream:
    artifact_receipt = client.put(
        f"/api/cad/fusion360/agent/artifacts/{plan['request_id']}/{path.name}",
        headers={
            "Content-Type": "model/step",
            "X-Artifact-Size": str(size),
            "X-Artifact-SHA256": sha256,
        },
        content=stream,
    ).raise_for_status().json()
```

Add-in 实现使用流式 hash/upload，不把完整文件读入内存；示例用 `read_bytes()` 只适合小测试文件。
文件内容、signature、size/hash、owner/request/filename 全部由服务端复核。
receipt 的 `download_url` 使用同一 bearer credential 执行 GET，不能公开成匿名静态 URL。

## 5. 可选 Core CadAdapter / Local Runtime API

如果部署选择 `mode=local_runtime`，Web/Backend 仍可调用统一接口，而不接触 Runtime 私有端点：

```text
GET  /api/cad/fusion360/status
GET  /api/cad/fusion360/capabilities
POST /api/cad/fusion360/context
POST /api/cad/fusion360/actions
POST /api/cad/fusion360/verify
GET  /api/cad/fusion360/requests/{request_id}
POST /api/cad/fusion360/requests/{request_id}/cancel
```

例如参数 preview：

```json
POST /api/cad/fusion360/actions
{
  "idempotency_key": "resize-width-workflow-42",
  "execution_mode": "preview",
  "action": "cad.update_parameter",
  "target": {
    "document_id": "doc-id",
    "component_id": "component-token",
    "parameter_id": "parameter-token"
  },
  "value": {"amount": 3, "unit": "mm"}
}
```

该兼容路径的 approval、lease、cancel、artifact proxy 细节见 API contract 第 2、8、10 节。
当前桌面交互的首选路径仍是 Palette -> HTTPS Cloud Agent -> typed proposal -> native Fusion API。
