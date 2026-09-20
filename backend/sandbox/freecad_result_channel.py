"""Invocation-bound result channel, independent of native stdout/stderr."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat


class ResultProtocolError(ValueError):
    pass


def publish_result(result: dict) -> None:
    raw_path = os.environ.get('CAD_FREECAD_RESULT_PATH')
    invocation = os.environ.get('CAD_FREECAD_INVOCATION_ID')
    if not raw_path or not invocation:
        # Direct developer invocations may still print diagnostics to stdout.
        # The capability dispatcher always supplies both values and requires
        # this file; it never falls back to interpreting those diagnostics.
        return
    path = Path(raw_path)
    temporary = path.with_suffix('.tmp')
    envelope = {'schema_version':'freecad-runner-envelope.v1',
                'invocation_id':invocation, 'result':result}
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(envelope, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_result(path: Path, invocation: str, *,
                success_schema: str = 'freecad-operation-result.v1') -> dict:
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'r', encoding='utf-8') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ResultProtocolError('FreeCAD result is not a regular file')
            envelope = json.load(stream)
        if (not isinstance(envelope, dict)
                or envelope.get('schema_version') != 'freecad-runner-envelope.v1'
                or envelope.get('invocation_id') != invocation):
            raise ResultProtocolError('FreeCAD result does not belong to this invocation')
        result = envelope.get('result')
        if not isinstance(result, dict) or result.get('status') not in {'succeeded','failed'}:
            raise ResultProtocolError('FreeCAD result has an invalid schema or status')
        expected_schema = success_schema if result['status'] == 'succeeded' else 'freecad-operation-result.v1'
        if result.get('schema_version') != expected_schema:
            raise ResultProtocolError('FreeCAD result has the wrong capability schema')
        return result
    except (OSError, ValueError) as exc:
        raise ResultProtocolError(f'FreeCAD structured result unavailable: {exc}') from exc
