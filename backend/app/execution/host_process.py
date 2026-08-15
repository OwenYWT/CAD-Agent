"""Reviewed host process supervisor for non-MCAD connectors and local UI tools.

MCAD computation must use ``ExecutionBackend``. This module exists only for
allowlisted connector CLIs and the loopback CAD viewer supervisor.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence


TimeoutExpired = subprocess.TimeoutExpired


def run(
    command: Sequence[str],
    *,
    cwd: str | Path,
    timeout: float,
):
    return subprocess.run(
        [str(part) for part in command],
        cwd=str(cwd),
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def start_detached(command: Sequence[str], *, cwd: str | Path):
    return subprocess.Popen(
        [str(part) for part in command],
        cwd=str(cwd),
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
