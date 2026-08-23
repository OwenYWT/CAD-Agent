# CAD-Agent Independent Evaluation Package

This directory is an independent evaluation handoff package. It is not part of
`CAD-Agent/`, but it can call the current CAD-Agent backend algorithm for batch
benchmarking.

## What is included

- `data/`: packaged CADPrompt and CAD-Coder evaluation data.
- `results/`: historical outputs and future run outputs.
- `evaluator.py`: base CLI for `reference` and `generate` passes.
- `evaluate_cadprompt.py`: CADPrompt end-to-end generation plus STL quality.
- `evaluate_cadcoder.py`: CAD-Coder reference baseline plus generation quality.
- `quality_eval.py`: STL mesh comparison and scoring utilities.
- `direct_algorithm.py`: recommended current-algorithm runner. It calls the
  CAD-Agent durable workflow path in-process.
- `eval_config.py`: centralized path configuration.

## Handoff reading order

1. `DATASETS.md`: datasets, file layout, and sample schema.
2. `EVALUATION_METHODS.md`: evaluation modes and commands.
3. `METRICS.md`: result fields and quality metrics.
4. `ALGORITHM_INTEGRATION.md`: how the CAD-Agent algorithm is connected.
5. `PREREQUISITES.md`: environment and runtime prerequisites.
6. `E2E_USAGE.md`: command cookbook for end-to-end runs.

## Smoke checks

```powershell
cd <eval_algorithms>
python evaluator.py --dataset both --mode generate --limit 1 --dry-run --output-dir results\handoff_smoke_eval
python evaluate_cadprompt.py --limit 1 --output-dir results\handoff_smoke_cadprompt --dry-run
python evaluate_cadcoder.py --limit 1 --output-dir results\handoff_smoke_cadcoder --dry-run
python -m unittest discover -s tests -p "test_*.py"
```

## Recommended current-algorithm runs

Use `--runner direct` unless you intentionally need REST API testing:

```powershell
cd <eval_algorithms>
python evaluate_cadprompt.py --runner direct --limit 2 --output-dir results\cadprompt_current_2
python evaluate_cadcoder.py --runner direct --limit 2 --output-dir results\cadcoder_current_2
```

If CAD-Agent is not located at the sibling path, set:

```powershell
$env:CAD_AGENT_ROOT="<path-to-CAD-Agent>"
```

## Important notes

- Existing folders under `results/` are historical outputs. Do not treat them as
  current-algorithm results unless their run command is known.
- The current default runner is not the old `Orchestrator` shortcut. It enters
  the current CAD-Agent durable workflow submission path.
- `--runner http` is kept for advanced REST checks, but current REST write APIs
  require durable revision identity. It is not the default handoff path.
