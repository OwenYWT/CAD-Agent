import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app.api.auth import verify_api_key
from app.dfm import rule_store

logger = logging.getLogger(__name__)
router = APIRouter()


class RuleUpdateBody(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    threshold_min: float | None = None
    threshold_max: float | None = None
    severity: Literal['critical', 'warning', 'info'] | None = None
    enabled: bool | None = None
    description: str | None = None
    suggestion_template: str | None = None


class CloneRuleSetBody(BaseModel):
    source_id: str
    new_id: str
    new_name: str


@router.get("/dfm/rules")
async def list_all_rule_sets(_=Depends(verify_api_key)):
    rule_sets = await rule_store.list_rule_sets()
    return [rs.model_dump() for rs in rule_sets]


@router.get("/dfm/rules/{process}")
async def get_rules_by_process(process: str, _=Depends(verify_api_key)):
    rules = await rule_store.get_rules_by_process(process)
    if not rules:
        raise HTTPException(404, f"No rules found for process: {process}")
    return [r.model_dump() for r in rules]


@router.put("/dfm/rules/{rule_id}")
async def update_rule(rule_id: str, body: RuleUpdateBody, _=Depends(verify_api_key)):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields to update")
    try:
        rule = await rule_store.update_rule(rule_id, updates)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not rule:
        raise HTTPException(404, f"Rule not found: {rule_id}")
    return rule.model_dump()


@router.post("/dfm/rule-sets")
async def clone_rule_set(body: CloneRuleSetBody, _=Depends(verify_api_key)):
    result = await rule_store.clone_rule_set(body.source_id, body.new_id, body.new_name)
    if not result:
        raise HTTPException(404, f"Source rule set not found: {body.source_id}")
    return result.model_dump()


@router.delete("/dfm/rule-sets/{set_id}")
async def delete_rule_set(set_id: str, _=Depends(verify_api_key)):
    if not await rule_store.delete_rule_set(set_id):
        raise HTTPException(400, "Cannot delete built-in rule sets")
    return {"ok": True}
