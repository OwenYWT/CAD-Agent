from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.agent import run_store
from app.agent.step_runner import decide_next_step

logger = logging.getLogger(__name__)


DEFAULT_STALE_AFTER_SECONDS = 15 * 60
EXECUTE_STEP_TYPES = {"execute_cad_code", "execute_code", "executing_code"}

RESUMABLE_MESSAGES = {
    "repair_succeeded": "\u4e0a\u6b21\u4efb\u52a1\u53ef\u7ee7\u7eed\uff1a\u81ea\u52a8\u4fee\u590d\u5df2\u5b8c\u6210\uff0c\u7b49\u5f85\u91cd\u65b0\u6267\u884c CAD \u4ee3\u7801\u3002",
    "execute_failed": "\u4e0a\u6b21\u4efb\u52a1\u53ef\u7ee7\u7eed\uff1aCAD \u6267\u884c\u5931\u8d25\uff0c\u53ef\u8fdb\u5165\u81ea\u52a8\u4fee\u590d\u3002",
    "execute_interrupted": "\u4e0a\u6b21\u4efb\u52a1\u6267\u884c\u4e2d\u65ad\uff0c\u53ef\u7ee7\u7eed\u91cd\u65b0\u6267\u884c CAD \u4ee3\u7801\u3002",
}

INTERRUPTED_RUNNING_MESSAGE = "\u4e0a\u6b21\u4efb\u52a1\u5728\u6267\u884c\u4e2d\u4e2d\u65ad\uff0c\u8bf7\u91cd\u65b0\u53d1\u8d77\u4efb\u52a1\u3002"
INCOMPLETE_STEP_ERROR = {
    "type": "IncompleteStep",
    "message": "\u5386\u53f2\u4efb\u52a1\u5b58\u5728\u672a\u5b8c\u6210\u6b65\u9aa4\uff0c\u5df2\u81ea\u52a8\u6e05\u7406\u4e3a\u5931\u8d25\u72b6\u6001\u3002",
}


def _cutoff_iso(stale_after_seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)).isoformat()


def interrupted_error(run: dict) -> dict:
    return {
        "type": "InterruptedRun",
        "message": "\u667a\u80fd\u4f53\u8fd0\u884c\u957f\u65f6\u95f4\u505c\u7559\u5728\u8fd0\u884c\u4e2d\uff0c\u5df2\u81ea\u52a8\u6062\u590d\u4e3a\u5931\u8d25\u72b6\u6001\uff0c\u8bf7\u91cd\u65b0\u53d1\u8d77\u4efb\u52a1\u3002",
        "run_id": run["id"],
    }


def _resume_running_step(step: dict[str, Any]) -> dict[str, Any] | None:
    if step.get("step_type") not in EXECUTE_STEP_TYPES:
        return None
    input_data = step.get("input") or {}
    code = input_data.get("code")
    if not code:
        return None
    resume_input = {
        "code": code,
        "output_formats": input_data.get("output_formats") or ["step", "stl"],
        "user_prompt": input_data.get("user_prompt") or "",
        "is_2d": bool(input_data.get("is_2d", False)),
    }
    if input_data.get("mode"):
        resume_input["mode"] = input_data["mode"]
    return {
        "resumable": True,
        "next_step": "execute_cad_code",
        "reason": "execute_interrupted",
        "input": resume_input,
        "interrupted_step_id": step.get("id"),
        "message": RESUMABLE_MESSAGES["execute_interrupted"],
    }



def _is_interrupted_step(step: dict[str, Any]) -> bool:
    error = step.get("error") or {}
    return error.get("type") in {"InterruptedRun", "IncompleteStep"}


def _resume_from_last_known_code(steps: list[dict[str, Any]], reason: str = "execute_interrupted") -> dict[str, Any] | None:
    for step in reversed(steps):
        if step.get("step_type") == "resume_available" and step.get("status") == "blocked":
            return None
        input_data = step.get("input") or {}
        output_data = step.get("output") or {}
        code = None
        if step.get("step_type") == "repair_code":
            code = output_data.get("repaired_code")
        if not code and step.get("step_type") in EXECUTE_STEP_TYPES:
            code = input_data.get("code")
        if not code:
            continue
        resume_input = {
            "code": code,
            "output_formats": input_data.get("output_formats") or ["step", "stl"],
            "user_prompt": input_data.get("user_prompt") or "",
            "is_2d": bool(input_data.get("is_2d", False)),
        }
        if input_data.get("mode"):
            resume_input["mode"] = input_data["mode"]
        return {
            "resumable": True,
            "next_step": "execute_cad_code",
            "reason": reason,
            "input": resume_input,
            "message": RESUMABLE_MESSAGES.get(reason, RESUMABLE_MESSAGES["execute_interrupted"]),
        }
    return None


def classify_resume_state(steps: list[dict[str, Any]]) -> dict[str, Any]:
    if not steps:
        return {
            "resumable": False,
            "next_step": None,
            "reason": "no_steps",
            "message": "\u4e0a\u6b21\u4efb\u52a1\u6ca1\u6709\u53ef\u6062\u590d\u7684\u6b65\u9aa4\uff0c\u8bf7\u91cd\u65b0\u53d1\u8d77\u4efb\u52a1\u3002",
        }

    latest = steps[-1]
    if latest.get("status") == "running":
        interrupted_state = _resume_running_step(latest)
        if interrupted_state:
            return interrupted_state
        return {
            "resumable": False,
            "next_step": None,
            "reason": "running_interrupted",
            "message": INTERRUPTED_RUNNING_MESSAGE,
        }

    if latest.get("status") == "failed" and _is_interrupted_step(latest):
        interrupted_resume = _resume_from_last_known_code(steps)
        if interrupted_resume:
            return interrupted_resume

    decision = decide_next_step(steps)
    is_resumable = decision.step_type == "execute_cad_code" and bool(decision.input_data.get("code"))
    return {
        "resumable": is_resumable,
        "next_step": decision.step_type,
        "reason": decision.reason,
        "input": decision.input_data,
        "message": RESUMABLE_MESSAGES.get(
            decision.reason,
            "\u4e0a\u6b21\u4efb\u52a1\u5df2\u6709\u660e\u786e\u7ed3\u679c\uff0c\u65e0\u9700\u7eed\u8dd1\u3002",
        ),
    }


async def recover_running_run(run: dict) -> int:
    if run.get("status") != "running":
        return 0
    steps = await run_store.list_steps(run["id"])
    resume_state = classify_resume_state(steps)
    if resume_state["resumable"]:
        interrupted_step_id = resume_state.get("interrupted_step_id")
        if interrupted_step_id:
            await run_store.fail_step(
                interrupted_step_id,
                interrupted_error(run),
                update_run_status=False,
            )
        await mark_resume_available(run, resume_state)
        return 1
    error = interrupted_error(run)
    await run_store.fail_run(run["id"], error)
    return 1


async def recover_running_runs() -> int:
    running_runs = await run_store.list_running_runs()
    recovered = 0
    for run in running_runs:
        recovered += await recover_running_run(run)
    if recovered:
        logger.info("Recovered %d running agent runs", recovered)
    return recovered


async def recover_stale_runs(stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS) -> int:
    stale_runs = await run_store.list_stale_running_runs(_cutoff_iso(stale_after_seconds))
    recovered = 0
    for run in stale_runs:
        recovered += await recover_running_run(run)
    if recovered:
        logger.info("Recovered %d stale running agent runs", recovered)
    return recovered


async def mark_resume_available(run: dict, resume_state: dict[str, Any]) -> None:
    step = await run_store.start_step(
        run["id"],
        "resume_available",
        {
            "reason": resume_state["reason"],
            "next_step": resume_state["next_step"],
            "resume_input": resume_state.get("input") or {},
        },
    )
    await run_store.complete_step(
        step["id"],
        {
            "resumable": True,
            "next_step": resume_state["next_step"],
            "message": resume_state["message"],
            "resume_input": resume_state.get("input") or {},
        },
        status="blocked",
    )
    await run_store.complete_run(run["id"], status="blocked", result_request_id=run.get("result_request_id"))


async def ensure_run_resume_available(run_id: str) -> int:
    run = await run_store.get_run(run_id)
    if not run or run["status"] not in {"running", "failed", "blocked"}:
        return 0
    steps = await run_store.list_steps(run_id)
    if any(step["step_type"] == "resume_available" and step["status"] == "blocked" for step in steps):
        return 0
    resume_state = classify_resume_state(steps)
    if not resume_state["resumable"]:
        return 0
    await mark_resume_available(run, resume_state)
    return 1


async def sanitize_incomplete_steps(run_id: str) -> int:
    run = await run_store.get_run(run_id)
    if not run or run["status"] not in {"succeeded", "failed", "blocked", "cancelled"}:
        return 0
    sanitized = 0
    for step in await run_store.list_steps(run_id):
        if step["status"] == "running":
            await run_store.fail_step(step["id"], INCOMPLETE_STEP_ERROR, update_run_status=False)
            sanitized += 1
    return sanitized
