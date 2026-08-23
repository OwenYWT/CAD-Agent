# Evaluation Data Directory

This directory contains the packaged datasets used by the independent CAD-Agent
evaluation harness.

## Summary

| Dataset | Path | Format | Local Count | Main Use |
| --- | --- | --- | ---: | --- |
| CADPrompt | `CADPrompt/` | One folder per CAD sample | 200 samples | Prompt generation and direct STL quality comparison |
| CAD-Coder | `CAD-coder/*.json` | JSON list of chat-style prompt/code records | 250,031 records across listed JSON files, see table below | Prompt generation, reference-code baseline, and generated-vs-reference quality comparison |

## CADPrompt

Path:

```text
CADPrompt/
```

Each CADPrompt sample is a directory named by sample ID, for example:

```text
CADPrompt/00002221/
  00002221.json
  Ground_Truth.json
  Ground_Truth.obj
  Ground_Truth.stl
  Natural_Language_Descriptions_Prompt.txt
  Natural_Language_Descriptions_Prompt_with_specific_measurements.txt
  Python_Code.py
```

### Local count

The current local copy contains **200 sample directories**.

All 200 samples contain the following files:

| File | Count | Meaning |
| --- | ---: | --- |
| `Natural_Language_Descriptions_Prompt.txt` | 200 | Natural-language prompt without extra measurement emphasis |
| `Natural_Language_Descriptions_Prompt_with_specific_measurements.txt` | 200 | Prompt with explicit dimensions/measurements; the evaluator reads this first |
| `Python_Code.py` | 200 | Dataset-provided CadQuery reference code |
| `Ground_Truth.stl` | 200 | Ground-truth mesh used for STL quality comparison |
| `Ground_Truth.obj` | 200 | Ground-truth OBJ mesh |
| `Ground_Truth.json` | 200 | Ground-truth metadata |

### Evaluator usage

- `load_cadprompt_samples(...)` reads prompt and reference code.
- `evaluate_cadprompt.py` runs current CAD-Agent generation and compares generated
  STL against `Ground_Truth.stl`.
- Prompt priority:
  1. `Natural_Language_Descriptions_Prompt_with_specific_measurements.txt`
  2. `Natural_Language_Descriptions_Prompt.txt`

## CAD-Coder

Path:

```text
CAD-coder/
```

CAD-Coder files are JSON arrays. Each array item is a chat-style record with a
natural-language user prompt and assistant-provided CadQuery code.

Typical record shape:

```json
{
  "model_path": "00357061.pth",
  "messages": [
    {"role": "user", "content": "Create a CAD model ..."},
    {"role": "assistant", "content": "```python\nimport cadquery as cq\n...\n```"}
  ]
}
```

Evaluator mapping:

- `sample_id`: file stem of `model_path`, for example `00357061`.
- `prompt`: `messages[0].content`.
- `reference_code`: `messages[1].content`; fenced Python blocks are automatically unwrapped.

### Local JSON files and counts

| File | Records | Size | Notes |
| --- | ---: | ---: | --- |
| `cad_data_train_high.json` | 8,177 | 17,882,505 bytes | Default CAD-Coder file used by the evaluator |
| `cad_data_validation.json` | 8,817 | 23,698,042 bytes | Validation split |
| `cad_data_train_middle.json` | 66,534 | 153,550,932 bytes | Larger training split |
| `cad_data_train_all.json` | 156,954 | 421,786,934 bytes | Full training split; protected by `--allow-large` |
| `cad_data_train_cot.json` | 1,503 | 11,033,774 bytes | Chain-of-thought style training subset |
| `cad_data_test_cot.json` | 8,046 | 38,196,084 bytes | Chain-of-thought style test subset |

Total records across the listed JSON files: **250,031**. Some files are different
splits/subsets and should not be blindly summed as unique CAD models.

### Evaluator usage

- `load_cadcoder_samples(...)` reads a selected JSON file.
- Default file: `CAD-coder/cad_data_train_high.json`.
- `evaluate_cadcoder.py` first executes dataset reference code to produce a
  reference STL, then runs current CAD-Agent generation, then compares generated
  STL against the reference STL.
- Large file guard: files above the configured large-file threshold require
  `--allow-large`.

## Path overrides

The evaluation harness resolves this directory through `eval_config.py`.

Useful environment variables:

```powershell
$env:EVAL_ALGORITHMS_DATA_ROOT="<path-to-data>"
$env:CADPROMPT_DIR="<path-to-CADPrompt>"
$env:CADCODER_FILE="<path-to-cad_data_train_high.json>"
```

## Recommended smoke checks

From `<eval_algorithms>`:

```powershell
python evaluator.py --dataset both --mode generate --limit 1 --dry-run --output-dir results\data_read_smoke
python evaluate_cadprompt.py --limit 1 --output-dir results\cadprompt_data_smoke --dry-run
python evaluate_cadcoder.py --limit 1 --output-dir results\cadcoder_data_smoke --dry-run
```
