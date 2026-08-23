import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eval_config
from evaluator import default_data_root


class EvalConfigTests(unittest.TestCase):
    def test_default_data_root_is_inside_independent_eval_directory(self):
        self.assertEqual(default_data_root(), eval_config.EVAL_ROOT / "data")

    def test_dataset_defaults_point_to_packaged_data(self):
        self.assertEqual(eval_config.default_cadprompt_dir(), eval_config.EVAL_ROOT / "data" / "CADPrompt")
        self.assertEqual(
            eval_config.default_cadcoder_file(),
            eval_config.EVAL_ROOT / "data" / "CAD-coder" / "cad_data_train_high.json",
        )

    def test_environment_overrides_data_root(self):
        old = os.environ.get("EVAL_ALGORITHMS_DATA_ROOT")
        try:
            os.environ["EVAL_ALGORITHMS_DATA_ROOT"] = str(Path("tmp/eval-data"))
            self.assertEqual(eval_config.default_data_root(), Path("tmp/eval-data").resolve())
        finally:
            if old is None:
                os.environ.pop("EVAL_ALGORITHMS_DATA_ROOT", None)
            else:
                os.environ["EVAL_ALGORITHMS_DATA_ROOT"] = old


if __name__ == "__main__":
    unittest.main()
