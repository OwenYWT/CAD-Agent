"""Overlap checker: are eval cases too close to the RAG library?

The eval is meant to be "open-book WITH RAG" (production-realistic) but NOT the
exact item the retriever already memorized. If a case's description is near-identical
to an example in backend/examples/*.json, the retriever hands the model the answer
and the case stops measuring anything. This tool flags such cases so they can be
reworded.

Uses the lightweight TF-IDF `ExampleRetriever` (not the ChromaDB vector retriever) so
it runs with no extra services and no network. The threshold is a heuristic on the
char-ngram cosine similarity; treat flagged cases as "review and reword", not a hard
gate.

Usage:
    cd backend && python -m benchmark.check_overlap [--threshold 0.5] [--top-k 3]

Exit code is non-zero if any case exceeds the threshold (handy for a pre-commit
check on the case set).
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from app.examples.retriever import ExampleRetriever
from benchmark.eval_cases import EVAL_CASES


async def _check(threshold: float, top_k: int) -> int:
    retriever = ExampleRetriever()
    if not retriever.examples:
        print("WARNING: no examples loaded — overlap check is meaningless.")
        return 0

    print(f"Checking {len(EVAL_CASES)} cases vs {len(retriever.examples)} RAG examples "
          f"(threshold={threshold}, top_k={top_k})\n")

    flagged = []
    for case in EVAL_CASES:
        hits = await retriever.find_similar(case["description"], top_k=top_k)
        top_score = hits[0]["score"] if hits else 0.0
        top_desc = hits[0]["description"] if hits else "—"
        mark = ""
        if top_score > threshold:
            flagged.append((case["id"], top_score, top_desc))
            mark = "  <-- OVERLAP"
        print(f"  {case['id']}: top={top_score:.3f}  «{top_desc[:42]}»{mark}")

    print()
    if flagged:
        print(f"⚠️  {len(flagged)} case(s) exceed similarity {threshold} — reword these:")
        for cid, score, desc in flagged:
            print(f"    {cid}  (sim={score:.3f})  closest example: «{desc[:60]}»")
        return 1

    print(f"✅ All cases below similarity {threshold}. No open-book overlap detected.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Check eval cases vs RAG example overlap.")
    ap.add_argument("--threshold", type=float, default=0.5,
                    help="flag cases whose top-1 example similarity exceeds this (default 0.5)")
    ap.add_argument("--top-k", type=int, default=3, help="retriever top_k (default 3)")
    args = ap.parse_args()
    sys.exit(asyncio.run(_check(args.threshold, args.top_k)))


if __name__ == "__main__":
    main()
