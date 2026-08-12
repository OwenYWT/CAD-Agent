import pytest

from app.workflows.agent_v2 import required_gate_blocks


@pytest.mark.parametrize("gate", ["visual", "dfm"])
@pytest.mark.parametrize("mode", ["required", "advisory"])
@pytest.mark.parametrize("outcome", ["passed", "failed", "indeterminate"])
def test_visual_and_dfm_gate_policy_matrix(gate, mode, outcome):
    del gate
    assert required_gate_blocks(mode, outcome) is (
        mode == "required" and outcome != "passed"
    )


@pytest.mark.parametrize(
    ("mode", "outcome"),
    [("disabled", "passed"), ("required", "unknown")],
)
def test_gate_policy_rejects_unrecognized_state(mode, outcome):
    with pytest.raises(ValueError):
        required_gate_blocks(mode, outcome)
