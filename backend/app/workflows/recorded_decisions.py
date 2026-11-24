"""Version a decision at the point of use and recover the unmarked pre-merge command."""
from temporalio import workflow

MERGE_FAILURE_PATCH = "agent-merge-failure-v1:"


def should_record_failure(error_code: str) -> bool:
    if not workflow.in_workflow():
        return False
    info = workflow.info()
    event = info.get_current_history_length()
    # An old run can reach a new decision after handoff. Scope the marker to
    # this task so an earlier unmarked wait or repair cannot pin later tasks.
    if workflow.patched(f"{MERGE_FAILURE_PATCH}{error_code}:{event}"):
        return True
    lookup = workflow.extern_functions().get("cad.recorded_merge_decision")
    return lookup is not None and lookup((info.run_id, event, error_code))
