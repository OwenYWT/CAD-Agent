import importlib.util
import json
from pathlib import Path
import shutil

import pytest

spec = importlib.util.spec_from_file_location('released_replay', Path(__file__).parent / 'e2e/replay_released_histories.py')
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def test_frozen_release_history_integrity_and_scenario_coverage():
    assert len(replay.load_histories()) >= 6


def test_corrupted_release_history_is_rejected(tmp_path):
    shutil.copytree(replay.FIXTURES, tmp_path / 'histories')
    root = tmp_path / 'histories'
    manifest = json.loads((root / 'manifest.json').read_text())
    path = root / manifest['histories'][0]['file']
    path.write_text(path.read_text() + ' ')
    with pytest.raises(ValueError, match='digest changed'):
        replay.load_histories(root)


def test_removed_release_scenario_is_rejected(tmp_path):
    shutil.copytree(replay.FIXTURES, tmp_path / 'histories')
    root = tmp_path / 'histories'
    path = root / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['histories'] = [row for row in manifest['histories'] if 'model-job-cancellation' not in row['cases']]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='missing released-history scenarios'):
        replay.load_histories(root)
