import pytest
from temporalio.activity import ActivityCancellationDetails

from app.execution.contracts import ExecutionStatus
from app.workflows.activities import interrupted_execution_status


@pytest.mark.parametrize("details,expected", [
    (None, ExecutionStatus.CANCELLED),
    (ActivityCancellationDetails(cancel_requested=True), ExecutionStatus.CANCELLED),
    (ActivityCancellationDetails(cancel_requested=True, timed_out=True), ExecutionStatus.CANCELLED),
    (ActivityCancellationDetails(timed_out=True), ExecutionStatus.TIMED_OUT),
    (ActivityCancellationDetails(worker_shutdown=True), ExecutionStatus.FAILED),
    (ActivityCancellationDetails(not_found=True), ExecutionStatus.FAILED),
    (ActivityCancellationDetails(reset=True), ExecutionStatus.FAILED),
    (ActivityCancellationDetails(paused=True), ExecutionStatus.FAILED),
])
def test_user_cancellation_and_retryable_infrastructure_interruption(details, expected):
    assert interrupted_execution_status(details) is expected
