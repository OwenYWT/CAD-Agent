# Evaluation Methods

## 1. Dry-run sample loading

Dry-run validates paths, sample selection, and output writing. It does not call
CAD-Agent.

```powershell
cd <eval_algorithms>
python evaluator.py --dataset both --mode generate --limit 2 --dry-run --output-dir results\dry_both_2
python evaluate_cadprompt.py --limit 2 --output-dir results\dry_cadprompt_2 --dry-run
python evaluate_cadcoder.py --limit 2 --output-dir results\dry_cadcoder_2 --dry-run
```

## 2. Reference baseline

Reference mode executes dataset-provided CadQuery code. It checks whether the
dataset baseline can export the requested files in the current backend runtime.

```powershell
python evaluator.py --dataset cadprompt --mode reference --limit 20 --output-dir results\cadprompt_reference_20
python evaluator.py --dataset cadcoder --mode reference --limit 20 --output-dir results\cadcoder_reference_20
```

## 3. Generation pass

Generate mode sends prompts to the current CAD-Agent algorithm and records
workflow, revision, files, inspection, repair, and error metadata.

```powershell
python evaluator.py --dataset cadprompt --mode generate --limit 20 --output-dir results\cadprompt_generate_20
python evaluator.py --dataset cadcoder --mode generate --limit 20 --output-dir results\cadcoder_generate_20
```

## 4. CADPrompt end-to-end evaluation

CADPrompt has ground-truth STL files, so generation can be compared directly to
`Ground_Truth.stl`.

```powershell
python evaluate_cadprompt.py --runner direct --limit 20 --output-dir results\cadprompt_e2e_current_20
```

## 5. CAD-Coder end-to-end evaluation

CAD-Coder does not ship ground-truth STL files. The end-to-end script:

1. Executes reference CadQuery code to produce reference STL.
2. Runs current CAD-Agent generation from prompt.
3. Compares generated STL against reference STL.

```powershell
python evaluate_cadcoder.py --runner direct --limit 20 --output-dir results\cadcoder_e2e_current_20
```

Generation-only run:

```powershell
python evaluate_cadcoder.py --runner direct --skip-reference --limit 20 --output-dir results\cadcoder_generation_only_20
```

Quality comparison is expected to fail without reference STL when
`--skip-reference` is used.

## 6. Resume behavior

`evaluator.py` skips completed `(dataset, sample_id, mode)` rows already present
in `results.jsonl`.

```powershell
python evaluator.py --dataset both --mode generate --limit 100 --output-dir results\generate_100 --max-pending 10
python evaluator.py --dataset both --mode generate --limit 100 --output-dir results\generate_100 --no-resume
```
