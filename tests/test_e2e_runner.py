import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluator import EvalSample
from e2e_runner import (
    select_samples,
    summarize_named_results,
    write_end_to_end_report,
    write_named_json_outputs,
)


class EndToEndRunnerTests(unittest.TestCase):
    def test_select_samples_filters_by_id_and_preserves_dataset_order(self):
        samples = [
            EvalSample("a", "CADPrompt", "prompt a", "code a", "src/a"),
            EvalSample("b", "CADPrompt", "prompt b", "code b", "src/b"),
            EvalSample("c", "CADPrompt", "prompt c", "code c", "src/c"),
        ]

        selected = select_samples(samples, sample_ids=["c", "a"])

        self.assertEqual([sample.sample_id for sample in selected], ["a", "c"])

    def test_write_named_json_outputs_uses_expected_file_names(self):
        rows = [
            {"dataset": "CADPrompt", "sample_id": "a", "success": True, "execution_time_ms": 10},
            {"dataset": "CADPrompt", "sample_id": "b", "success": False, "error_type": "Timeout", "execution_time_ms": 20},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            summary = write_named_json_outputs(output_dir, "generation", rows)

            self.assertEqual(summary["total"], 2)
            self.assertEqual(summary["success"], 1)
            self.assertTrue((output_dir / "generation_results.jsonl").exists())
            self.assertTrue((output_dir / "generation_summary.json").exists())
            self.assertTrue((output_dir / "generation_failures.csv").exists())

    def test_write_end_to_end_report_includes_generation_reference_and_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            write_end_to_end_report(
                output_dir=output_dir,
                dataset="CAD-Coder",
                selected_count=3,
                generation_summary={"total": 3, "success": 2, "failed": 1, "success_rate": 2 / 3},
                quality_summary={"quality_success": 2, "quality_failed": 1, "avg_quality_score": 0.75},
                reference_summary={"total": 3, "success": 3, "failed": 0, "success_rate": 1.0},
            )

            report = (output_dir / "report.md").read_text(encoding="utf-8")
            self.assertIn("CAD-Coder", report)
            self.assertIn("Generation", report)
            self.assertIn("Reference", report)
            self.assertIn("Quality", report)

    def test_summarize_named_results_accepts_empty_rows(self):
        self.assertEqual(summarize_named_results([])["total"], 0)


if __name__ == "__main__":
    unittest.main()
