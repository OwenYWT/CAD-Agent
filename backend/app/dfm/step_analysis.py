"""Host-side STEP analysis executor — sends analysis script to sandbox."""

import json
import logging
from pathlib import Path

from app.dfm.models import StepAnalysisResult
from app.dfm.step_analyzer_script import STEP_ANALYSIS_SCRIPT
from app.execution.compat_executor import CompatibilityExecutor
from app.execution.composition import get_execution_backend

logger = logging.getLogger(__name__)


class StepAnalyzer:
    """Runs OCP-based STEP analysis inside the sandbox container."""

    def __init__(self, executor=None, *, execution_backend=None):
        self._executor = executor or CompatibilityExecutor(
            execution_backend or get_execution_backend()
        )

    @property
    def executor(self):
        return self._executor

    async def analyze(self, step_path: Path) -> StepAnalysisResult:
        """Send STEP file + analysis script to sandbox, return parsed result."""
        if not step_path.exists():
            return StepAnalysisResult(error=f"STEP file not found: {step_path}")

        result = await self.executor.execute(
            code=STEP_ANALYSIS_SCRIPT,
            mode="analysis",
            extra_files={"model.step": step_path},
        )

        if not result.success:
            msg = result.error_message or "Unknown sandbox error"
            logger.warning(f"STEP analysis sandbox error: {msg}")
            if result.traceback:
                logger.debug(f"Traceback:\n{result.traceback}")
            return StepAnalysisResult(error=msg)

        # Read analysis.json from output
        analysis_file = result.files.get("analysis.json")
        if not analysis_file or not analysis_file.exists():
            return StepAnalysisResult(error="Sandbox produced no analysis.json")

        try:
            data = json.loads(analysis_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            return StepAnalysisResult(error=f"Failed to parse analysis output: {e}")

        if "error" in data and data["error"]:
            return StepAnalysisResult(error=data["error"])

        try:
            return StepAnalysisResult(**data)
        except Exception as e:
            logger.warning(f"Failed to parse STEP analysis result: {e}")
            return StepAnalysisResult(error=f"Result parsing error: {e}")
