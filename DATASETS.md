# Dataset Handoff

## Default paths

Path defaults are centralized in `eval_config.py`.

| Item | Default |
| --- | --- |
| Eval root | `<eval_algorithms>` |
| Data root | `eval_algorithms\data` |
| CADPrompt | `eval_algorithms\data\CADPrompt` |
| CAD-Coder | `eval_algorithms\data\CAD-coder\cad_data_train_high.json` |
| Results | `eval_algorithms\results` |
| CAD-Agent root | sibling `<CAD-Agent>` |

Environment overrides:

```powershell
$env:EVAL_ALGORITHMS_DATA_ROOT="<path-to-data>"
$env:CADPROMPT_DIR="<path-to-CADPrompt>"
$env:CADCODER_FILE="<path-to-cad_data_train_high.json>"
$env:CAD_AGENT_ROOT="<path-to-CAD-Agent>"
$env:CAD_AGENT_STORAGE_ROOT="<path-to-CAD-Agent>\backend\data\files"
```

## CADPrompt structure

Each sample is a directory:

```text
data\CADPrompt\00002221\
  Natural_Language_Descriptions_Prompt.txt
  Natural_Language_Descriptions_Prompt_with_specific_measurements.txt
  Python_Code.py
  Ground_Truth.stl
  Ground_Truth.obj
  Ground_Truth.json
  00002221.json
```

Loader behavior:

1. Prompt uses `Natural_Language_Descriptions_Prompt_with_specific_measurements.txt` first.
2. If it is missing, prompt falls back to `Natural_Language_Descriptions_Prompt.txt`.
3. Reference code uses `Python_Code.py`.
4. Quality comparison uses `Ground_Truth.stl`.

Current local CADPrompt count is about 200 sample directories.

## CAD-Coder structure

Default file:

```text
data\CAD-coder\cad_data_train_high.json
```

Other available files:

- `cad_data_validation.json`
- `cad_data_train_middle.json`
- `cad_data_train_all.json`
- `cad_data_train_cot.json`
- `cad_data_test_cot.json`

Record schema used by the loader:

```json
{
  "model_path": "00357061.pth",
  "messages": [
    {"role": "user", "content": "prompt text"},
    {"role": "assistant", "content": "CadQuery code or fenced python code"}
  ]
}
```

Loader behavior:

- `sample_id` is `Path(model_path).stem`.
- Prompt is `messages[0].content`.
- Reference code is extracted from `messages[1].content`.
- Fenced Python code blocks are unwrapped automatically.

## Large file guard

Very large CAD-Coder files are protected by default. Use `--allow-large` only
when intentionally loading them:

```powershell
python evaluator.py --dataset cadcoder --cadcoder-file data\CAD-coder\cad_data_train_all.json --allow-large
```
