"""Replay actual model output across independently reopened native checkpoints."""
import os
import runpy
from pathlib import Path

root = Path(globals().get('__file__', '/tests/freecad_checkpoint_replay_contract.py')).parent
os.environ['CAD_TOOL_SOURCE_EVIDENCE'] = str(root / 'fixtures/live_freecad_checkpoint_plate_20260928.json')
os.environ['CAD_TOOL_REPLAY_MARKER'] = 'CAD_CHECKPOINT_REPLAY_CONTRACT='
runpy.run_path(str(root / 'freecad_tool_replay_contract.py'), run_name='__main__')
