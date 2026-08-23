import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluator import (
    EvalSample,
    append_result,
    completed_keys,
    evaluate_sample,
    adapt_reference_code,
    load_cadcoder_samples,
    load_cadprompt_samples,
    summarize_results,
)


class EvaluatorTests(unittest.TestCase):
    def test_load_cadprompt_samples_reads_prompt_and_reference_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            sample_dir = Path(tmp) / "CADPrompt" / "00000001"
            sample_dir.mkdir(parents=True)
            prompt_path = sample_dir / "Natural_Language_Descriptions_Prompt_with_specific_measurements.txt"
            prompt_path.write_text("Create a bracket with two perpendicular plates.", encoding="utf-8")
            (sample_dir / "Natural_Language_Descriptions_Prompt.txt").write_text("Create a bracket.", encoding="utf-8")
            (sample_dir / "Python_Code.py").write_text("part = cq.Workplane('XY').box(1, 2, 3)", encoding="utf-8")

            samples = load_cadprompt_samples(sample_dir.parent, limit=10)

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].sample_id, "00000001")
        self.assertEqual(samples[0].dataset, "CADPrompt")
        self.assertIn("perpendicular plates", samples[0].prompt)
        self.assertIn("part =", samples[0].reference_code)

    def test_load_cadcoder_samples_reads_messages_and_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cad_data_train_high.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "model_path": "00000001.pth",
                            "messages": [
                                {"role": "user", "content": "Create a cylinder."},
                                {"role": "assistant", "content": "import cadquery as cq\nr = cq.Workplane('XY').circle(1).extrude(2)"},
                            ],
                        },
                        {
                            "model_path": "00000002.pth",
                            "messages": [
                                {"role": "user", "content": "Create a box."},
                                {"role": "assistant", "content": "import cadquery as cq\nr = cq.Workplane('XY').box(1, 1, 1)"},
                            ],
                        },
                    ]
                ),
                encoding="utf-8",
            )

            samples = load_cadcoder_samples(path, limit=1)

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].sample_id, "00000001")
        self.assertEqual(samples[0].dataset, "CAD-Coder")
        self.assertEqual(samples[0].prompt, "Create a cylinder.")
        self.assertIn("r =", samples[0].reference_code)

    def test_adapt_reference_code_exposes_common_final_variables_as_result(self):
        r_code = adapt_reference_code("import cadquery as cq\nr = cq.Workplane('XY').box(1, 1, 1)")
        part_code = adapt_reference_code("import cadquery as cq\npart = cq.Workplane('XY').box(1, 1, 1)")
        result_code = adapt_reference_code("import cadquery as cq\nresult = cq.Workplane('XY').box(1, 1, 1)")

        self.assertTrue(r_code.rstrip().endswith("result = r"))
        self.assertTrue(part_code.rstrip().endswith("result = part"))
        self.assertEqual(result_code.count("result ="), 1)

    def test_summarize_results_counts_success_and_error_types(self):
        summary = summarize_results(
            [
                {"success": True, "execution_time_ms": 120, "error_type": None},
                {"success": False, "execution_time_ms": 50, "error_type": "ValidationError"},
                {"success": False, "execution_time_ms": 70, "error_type": "ValidationError"},
            ]
        )

        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["success"], 1)
        self.assertAlmostEqual(summary["success_rate"], 1 / 3)
        self.assertEqual(summary["error_types"], {"ValidationError": 2})
        self.assertEqual(summary["avg_execution_time_ms"], 80)


    def test_evaluate_sample_records_durable_metadata(self):
        class FakeClient:
            def post_json(self, path, payload):
                self.path = path
                self.payload = payload
                return {
                    "success": True,
                    "request_id": "run-1",
                    "workflow_run_id": "run-1",
                    "task_status": "succeeded",
                    "project_id": "project-1",
                    "branch_id": "branch-1",
                    "expected_base_revision_id": "base-1",
                    "revision_id": "revision-1",
                    "change_set_id": "change-1",
                    "files": {"stl": "/api/files/run-1/result.stl"},
                    "inspect_report": {"verdict": "pass"},
                    "repair_history": [],
                    "validation": {"ok": True},
                }

        sample = EvalSample(
            "00000001", "CADPrompt", "make box", "result = box", "source"
        )
        client = FakeClient()
        row = evaluate_sample(client, sample, "generate", ["stl"])

        self.assertEqual(client.path, "/api/generate")
        self.assertEqual(client.payload["sample_id"], "00000001")
        self.assertEqual(row["workflow_run_id"], "run-1")
        self.assertEqual(row["revision_id"], "revision-1")
        self.assertEqual(row["files"], {"stl": "/api/files/run-1/result.stl"})
        self.assertEqual(row["inspect_report"], {"verdict": "pass"})

    def test_append_result_flushes_jsonl_and_completed_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            row = {
                "dataset": "CADPrompt",
                "sample_id": "00000001",
                "mode": "generate",
                "success": True,
            }

            append_result(output_dir, row)
            append_result(output_dir, {**row, "sample_id": "00000002"})

            lines = (output_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(completed_keys(output_dir), {("CADPrompt", "00000001", "generate"), ("CADPrompt", "00000002", "generate")})


if __name__ == "__main__":
    unittest.main()
