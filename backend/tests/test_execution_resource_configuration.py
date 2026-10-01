"""Deployment resource settings must reach every actual execution phase."""
import ast
from pathlib import Path
import pytest
from app.execution.contracts import ResourceLimits

@pytest.mark.parametrize('value,wanted',[('512m',512*1024**2),('3g',3*1024**3),('1.5g',1536*1024**2),('3221225472',3*1024**3)])
def test_configured_memory_reaches_execution_envelope(value,wanted):
    result=ResourceLimits.from_configured_memory(value,timeout_seconds=120)
    assert result.memory_bytes==wanted
    assert result.timeout_seconds==120

def test_existing_operation_allocation_is_not_reduced_by_default_config():
    result=ResourceLimits.from_configured_memory('512m',memory_bytes=1536*1024**2)
    assert result.memory_bytes==1536*1024**2

@pytest.mark.parametrize('value',['unlimited','-1','NaN','0','0.0001b'])
def test_invalid_memory_configuration_is_explicitly_rejected(value):
    with pytest.raises(ValueError):ResourceLimits.from_configured_memory(value)

def test_execution_composition_never_silently_uses_wire_memory_default():
    root=Path(__file__).resolve().parents[1]/'app'
    paths=[*root.glob('workflows/handlers/*.py'),root/'execution/compat_executor.py',root/'execution/capability_adapter.py']
    violations=[]
    for p in paths:
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='ResourceLimits':
                violations.append((p.name,node.lineno))
    assert not violations,violations
