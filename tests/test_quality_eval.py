import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import trimesh

from quality_eval import (
    compare_meshes,
    file_url_to_local_path,
    score_from_relative_error,
    write_quality_outputs,
)


class QualityEvalTests(unittest.TestCase):
    def test_score_from_relative_error_clamps_to_zero_one(self):
        self.assertEqual(score_from_relative_error(0.0, tolerance=0.25), 1.0)
        self.assertEqual(score_from_relative_error(0.25, tolerance=0.25), 0.0)
        self.assertEqual(score_from_relative_error(0.5, tolerance=0.25), 0.0)

    def test_file_url_to_local_path_maps_backend_file_url(self):
        path = file_url_to_local_path(
            "/api/files/request-1/result.stl",
            storage_root=Path("backend/data/files"),
        )
        self.assertEqual(path, Path("backend/data/files/request-1/result.stl"))

    def test_compare_meshes_scores_identical_mesh_high(self):
        mesh = trimesh.creation.box(extents=(1, 2, 3))
        metrics = compare_meshes(mesh, mesh.copy(), sample_count=256)

        self.assertTrue(metrics["generated_watertight"])
        self.assertEqual(metrics["bbox_relative_error"], 0.0)
        self.assertEqual(metrics["volume_relative_error"], 0.0)
        self.assertGreater(metrics["quality_score"], 0.95)

    def test_compare_meshes_penalizes_different_dimensions(self):
        reference = trimesh.creation.box(extents=(1, 1, 1))
        generated = trimesh.creation.box(extents=(2, 1, 1))
        metrics = compare_meshes(reference, generated, sample_count=256)

        self.assertGreater(metrics["bbox_relative_error"], 0.2)
        self.assertGreater(metrics["volume_relative_error"], 0.5)
        self.assertLess(metrics["quality_score"], 0.9)


    def test_compare_meshes_separates_shape_from_strict_dimensions(self):
        reference = trimesh.creation.box(extents=(1, 2, 3))
        generated = trimesh.creation.box(extents=(10, 20, 30))
        metrics = compare_meshes(reference, generated, sample_count=256)

        self.assertGreater(metrics["shape_score"], 0.9)
        self.assertLess(metrics["dimension_score"], 0.2)
        self.assertGreater(metrics["bbox_relative_error"], 5.0)

    def test_write_quality_outputs_creates_jsonl_summary_and_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            rows = [
                {"dataset": "CADPrompt", "sample_id": "a", "quality_score": 1.0, "success": True},
                {"dataset": "CADPrompt", "sample_id": "b", "quality_score": 0.0, "success": False, "error_type": "MissingFile"},
            ]

            summary = write_quality_outputs(rows, output_dir)

            self.assertEqual(summary["total"], 2)
            self.assertEqual(summary["quality_success"], 1)
            self.assertTrue((output_dir / "quality_results.jsonl").exists())
            self.assertTrue((output_dir / "quality_summary.json").exists())
            self.assertTrue((output_dir / "quality_failures.csv").exists())


if __name__ == "__main__":
    unittest.main()
