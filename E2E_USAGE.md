# End-to-End Usage Cookbook

Run commands from the independent evaluation directory:

```powershell
cd <eval_algorithms>
```

## CADPrompt dry run

```powershell
python evaluate_cadprompt.py --limit 5 --output-dir results\dry_cadprompt_5 --dry-run
```

## CADPrompt current algorithm

```powershell
python evaluate_cadprompt.py --runner direct --limit 5 --output-dir results\cadprompt_current_5
```

Specific samples:

```powershell
python evaluate_cadprompt.py --runner direct --sample-ids 00002221 00004935 --output-dir results\cadprompt_selected
```

## CAD-Coder dry run

```powershell
python evaluate_cadcoder.py --limit 5 --output-dir results\dry_cadcoder_5 --dry-run
```

## CAD-Coder current algorithm with reference baseline

```powershell
python evaluate_cadcoder.py --runner direct --limit 5 --output-dir results\cadcoder_current_5
```

Specific samples:

```powershell
python evaluate_cadcoder.py --runner direct --sample-ids 00357061 00352432 --output-dir results\cadcoder_selected
```

## Base evaluator commands

Reference mode:

```powershell
python evaluator.py --dataset cadprompt --mode reference --limit 20 --output-dir results\cadprompt_reference_20
python evaluator.py --dataset cadcoder --mode reference --limit 20 --output-dir results\cadcoder_reference_20
```

Generation mode:

```powershell
python evaluator.py --dataset cadprompt --mode generate --limit 20 --output-dir results\cadprompt_generate_20
python evaluator.py --dataset cadcoder --mode generate --limit 20 --output-dir results\cadcoder_generate_20
```

## Output files

End-to-end output directories contain:

- `selected_samples.json`
- `generation_results.jsonl`
- `generation_summary.json`
- `generation_failures.csv`
- `quality_results.jsonl`
- `quality_summary.json`
- `quality_failures.csv`
- `report.md`
- `end_to_end_summary.json`

CAD-Coder also contains:

- `reference_results.jsonl`
- `reference_summary.json`
- `reference_failures.csv`

## Runner modes

- `--runner direct`: recommended. Calls current CAD-Agent durable workflow code
  in-process.
- `--runner http`: advanced REST test mode. Current REST writes require durable
  identity, so this is not the default handoff path.
