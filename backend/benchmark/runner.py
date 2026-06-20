"""
Benchmark runner for CAD Agent quality evaluation.

Usage:
    cd backend && DASHSCOPE_API_KEY=$DASHSCOPE_API_KEY python -m benchmark.runner
"""
import asyncio
import json
import logging
import sys
import time
from collections import defaultdict

from benchmark.cases import BENCHMARK_CASES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


class BenchmarkRunner:
    async def run(self, cases: list[dict] | None = None) -> dict:
        from app.agent.orchestrator import Orchestrator
        from app.validation.geometry_validator import GeometryValidator

        cases = cases or BENCHMARK_CASES
        geo_validator = GeometryValidator()

        results = []
        by_difficulty: dict[str, dict] = defaultdict(lambda: {"total": 0, "success": 0})
        dim_checks = 0
        dim_matches = 0
        total_time = 0

        for i, case in enumerate(cases):
            case_id = case["id"]
            desc = case["description"]
            difficulty = case["difficulty"]
            expected = case.get("expected_dims", {})

            logger.info(f"[{i + 1}/{len(cases)}] {case_id}: {desc}")

            by_difficulty[difficulty]["total"] += 1
            start = time.time()

            try:
                orchestrator = Orchestrator()
                response = await orchestrator.generate(desc, output_formats=["step", "stl"])
                elapsed_ms = int((time.time() - start) * 1000)
                total_time += elapsed_ms

                detail = {
                    "id": case_id,
                    "success": response.success,
                    "attempts": response.attempts,
                    "time_ms": elapsed_ms,
                    "error": str(response.error) if response.error else None,
                }

                if response.success:
                    by_difficulty[difficulty]["success"] += 1

                    # Dimension check if expected_dims provided
                    if expected and response.validation and response.validation.bounding_box:
                        dim_checks += 1
                        bb = response.validation.bounding_box
                        dx = bb.x_max - bb.x_min
                        dy = bb.y_max - bb.y_min
                        dz = bb.z_max - bb.z_min
                        actual_sorted = sorted([dx, dy, dz], reverse=True)
                        expected_sorted = sorted(
                            [expected.get("width", 0), expected.get("height", 0), expected.get("depth", 0)],
                            reverse=True,
                        )
                        # Within 10% tolerance
                        match = all(
                            abs(a - e) / max(e, 1) < 0.10
                            for a, e in zip(actual_sorted, expected_sorted)
                            if e > 0
                        )
                        if match:
                            dim_matches += 1
                        detail["dimension_match"] = match

                results.append(detail)
                status = "OK" if response.success else "FAIL"
                logger.info(f"  → {status} ({elapsed_ms}ms, attempts={response.attempts})")

            except Exception as e:
                elapsed_ms = int((time.time() - start) * 1000)
                total_time += elapsed_ms
                logger.error(f"  → ERROR: {e}")
                results.append({
                    "id": case_id,
                    "success": False,
                    "attempts": 0,
                    "time_ms": elapsed_ms,
                    "error": str(e),
                })

        total = len(cases)
        success = sum(1 for r in results if r["success"])

        summary = {
            "total": total,
            "success": success,
            "success_rate": round(success / total, 2) if total > 0 else 0,
            "by_difficulty": {},
            "avg_time_ms": round(total_time / total) if total > 0 else 0,
            "dimension_match_rate": round(dim_matches / dim_checks, 2) if dim_checks > 0 else None,
            "details": results,
        }

        for diff, counts in by_difficulty.items():
            t, s = counts["total"], counts["success"]
            summary["by_difficulty"][diff] = {
                "total": t,
                "success": s,
                "rate": round(s / t, 2) if t > 0 else 0,
            }

        return summary


async def main():
    runner = BenchmarkRunner()
    results = await runner.run()

    print("\n" + "=" * 60)
    print("BENCHMARK RESULTS")
    print("=" * 60)
    print(f"Total: {results['total']}, Success: {results['success']}, Rate: {results['success_rate']:.0%}")
    print(f"Avg time: {results['avg_time_ms']}ms")
    if results["dimension_match_rate"] is not None:
        print(f"Dimension match: {results['dimension_match_rate']:.0%}")

    for diff in ["simple", "moderate", "complex"]:
        if diff in results["by_difficulty"]:
            d = results["by_difficulty"][diff]
            print(f"  {diff}: {d['success']}/{d['total']} ({d['rate']:.0%})")

    print("\nDetails:")
    for d in results["details"]:
        status = "OK" if d["success"] else "FAIL"
        print(f"  {d['id']}: {status} ({d['time_ms']}ms, attempts={d['attempts']})")

    # Save to file
    with open("benchmark_results.json", "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to benchmark_results.json")


if __name__ == "__main__":
    asyncio.run(main())
