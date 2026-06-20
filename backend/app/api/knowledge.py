"""Knowledge graph REST API — process/material/supplier management."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.auth import verify_api_key
from app.dfm.knowledge_graph import (
    KGNode,
    KGEdge,
    list_nodes,
    get_node,
    get_edges,
    get_process_constraints,
    get_supported_materials,
    recommend_processes,
    upsert_node,
    add_edge,
    delete_node,
    delete_edge,
    clone_for_customer,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# --- Request Models ---


class UpsertNodeRequest(BaseModel):
    id: str
    type: str
    name: str
    properties: dict = {}
    customer_id: str = "default"


class AddEdgeRequest(BaseModel):
    source_id: str
    target_id: str
    relationship: str
    properties: dict = {}
    customer_id: str = "default"


class RecommendRequest(BaseModel):
    max_dimension: float | None = None
    material: str | None = None
    customer_id: str = "default"


# --- Endpoints ---


@router.get("/knowledge/nodes")
async def api_list_nodes(
    type: str | None = None,
    customer_id: str = "default",
    _=Depends(verify_api_key),
):
    return await list_nodes(node_type=type, customer_id=customer_id)


@router.get("/knowledge/nodes/{node_id}")
async def api_get_node(
    node_id: str,
    customer_id: str = "default",
    _=Depends(verify_api_key),
):
    node = await get_node(node_id, customer_id)
    if not node:
        raise HTTPException(404, "Node not found")
    return node


@router.get("/knowledge/edges")
async def api_list_edges(
    source_id: str | None = None,
    target_id: str | None = None,
    relationship: str | None = None,
    customer_id: str = "default",
    _=Depends(verify_api_key),
):
    return await get_edges(source_id, target_id, relationship, customer_id)


@router.get("/knowledge/process/{process_id}/constraints")
async def api_process_constraints(
    process_id: str,
    material_id: str | None = None,
    customer_id: str = "default",
    _=Depends(verify_api_key),
):
    return await get_process_constraints(process_id, material_id, customer_id)


@router.get("/knowledge/process/{process_id}/materials")
async def api_process_materials(
    process_id: str,
    customer_id: str = "default",
    _=Depends(verify_api_key),
):
    return await get_supported_materials(process_id, customer_id)


@router.post("/knowledge/recommend")
async def api_recommend(
    body: RecommendRequest,
    _=Depends(verify_api_key),
):
    return await recommend_processes(
        max_dimension=body.max_dimension,
        material_preference=body.material,
        customer_id=body.customer_id,
    )


@router.post("/knowledge/nodes")
async def api_upsert_node(
    body: UpsertNodeRequest,
    _=Depends(verify_api_key),
):
    node = KGNode(**body.model_dump())
    return await upsert_node(node)


@router.post("/knowledge/edges")
async def api_add_edge(
    body: AddEdgeRequest,
    _=Depends(verify_api_key),
):
    edge = KGEdge(**body.model_dump())
    return await add_edge(edge)


@router.delete("/knowledge/nodes/{node_id}")
async def api_delete_node(
    node_id: str,
    customer_id: str = "default",
    _=Depends(verify_api_key),
):
    await delete_node(node_id, customer_id)
    return {"ok": True}


@router.delete("/knowledge/edges/{edge_id}")
async def api_delete_edge(
    edge_id: int,
    _=Depends(verify_api_key),
):
    await delete_edge(edge_id)
    return {"ok": True}


@router.post("/knowledge/clone/{customer_id}")
async def api_clone_for_customer(
    customer_id: str,
    _=Depends(verify_api_key),
):
    count = await clone_for_customer(customer_id)
    if count == 0:
        return {"message": "Customer already has knowledge graph", "count": 0}
    return {"message": f"Cloned {count} nodes for customer {customer_id}", "count": count}
