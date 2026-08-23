import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from direct_algorithm import DirectAlgorithmClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self):
        return self.payload


class FakeOrchestrator:
    async def generate(self, prompt, output_formats):
        return FakeResponse({"success": True, "kind": "generate", "prompt": prompt, "output_formats": output_formats})

    async def execute_code(self, code, output_formats):
        return FakeResponse({"success": True, "kind": "execute", "code": code, "output_formats": output_formats})


class DirectAlgorithmClientTests(unittest.TestCase):
    def test_post_json_generate_calls_orchestrator_without_http_auth(self):
        client = DirectAlgorithmClient(orchestrator=FakeOrchestrator())

        result = client.post_json("/api/generate", {"prompt": "make box", "output_formats": ["stl"]})

        self.assertTrue(result["success"])
        self.assertEqual(result["kind"], "generate")
        self.assertEqual(result["prompt"], "make box")

    def test_post_json_execute_calls_orchestrator_without_http_auth(self):
        client = DirectAlgorithmClient(orchestrator=FakeOrchestrator())

        result = client.post_json("/api/execute", {"code": "result = 1", "output_formats": ["step"]})

        self.assertTrue(result["success"])
        self.assertEqual(result["kind"], "execute")
        self.assertEqual(result["code"], "result = 1")

    def test_unknown_route_returns_error_payload(self):
        client = DirectAlgorithmClient(orchestrator=FakeOrchestrator())

        result = client.post_json("/api/unknown", {})

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["type"], "ValueError")


if __name__ == "__main__":
    unittest.main()
