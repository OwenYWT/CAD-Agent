---
license: apache-2.0
task_categories:
  - text-generation
language:
  - en
tags:
  - cad
  - code-generation
  - text-to-cad
  - cadquery
  - 3d-modeling
size_categories:
  - 100K<n<1M
pretty_name: CAD-Coder Dataset
---

# CAD-Coder Dataset

This is the official dataset for the paper **"CAD-Coder: Text-to-CAD Generation with Chain-of-Thought and Geometric Reward"**.

**Accepted at NeurIPS 2025 (Poster)**

## Dataset Description

CAD-Coder Dataset is a large-scale Text-to-CadQuery dataset containing natural language descriptions of 3D CAD models paired with executable [CadQuery](https://github.com/CadQuery/cadquery) Python code. The dataset enables training and evaluating language models to generate parametric CAD code from textual descriptions.

The dataset is synthesized based on [Text2CAD 1.0](https://huggingface.co/datasets/SadilKhan/Text2CAD) and includes:
- Detailed geometric descriptions (coordinates, Euler angles, sketch loops, extrusion parameters)
- Executable CadQuery Python scripts that generate corresponding 3D models
- Chain-of-Thought (CoT) reasoning data for enhanced model training

- **Paper:** [arXiv:2505.19713](https://arxiv.org/abs/2505.19713)
- **License:** Apache 2.0
- **Language:** English
- **Task:** Text-to-CadQuery Code Generation

## Dataset Statistics

| File | Description | Samples |
|------|-------------|---------|
| `cad_data_train_all.json` | All synthesized training data based on Text2CAD (not fully used in actual SFT training) | 156,954 |
| `cad_data_train_high.json` | High-quality synthesized data based on Text2CAD (used for actual SFT training) | 8,177 |
| `cad_data_train_middle.json` | Medium-quality synthesized data based on Text2CAD | 66,534 |
| `cad_data_train_cot.json` | Chain-of-Thought SFT data for cold-start training before GRPO | 1,503 |
| `cad_data_validation.json` | Validation set synthesized based on Text2CAD | 8,817 |
| `cad_data_test_cot.json` | Test set with CoT system prompt for batch inference evaluation | 8,046 |

**Total samples:** 250,031

## Data Format

Each JSON file contains a list of samples with the following structure:

```json
{
  "messages": [
    {
      "role": "user",
      "content": "Natural language description of the CAD model..."
    },
    {
      "role": "assistant",
      "content": "CadQuery Python code to generate the model..."
    }
  ],
  "model_path": "00008905.pth"
}
```

### Field Descriptions

| Field | Type | Description |
|-------|------|-------------|
| `messages` | List | Conversation format with user description and assistant response |
| `messages[0].role` | String | Always "user" |
| `messages[0].content` | String | Natural language description of the 3D CAD model |
| `messages[1].role` | String | Always "assistant" |
| `messages[1].content` | String | Executable CadQuery Python code |
| `model_path` | String | Reference identifier for the original 3D model |


## Citation

If you use this dataset in your research, please cite our paper:

```bibtex
@article{guan2025cadcoder,
  title={CAD-Coder: Text-to-CAD Generation with Chain-of-Thought and Geometric Reward},
  author={Guan, Yandong and Wang, Xilin and Xing, Ximing and Zhang, Jing and Xu, Dong and Yu, Qian},
  journal={arXiv preprint arXiv:2505.19713},
  year={2025}
}
```

## Acknowledgements

This dataset is built upon [Text2CAD](https://github.com/sadilkhan/Text2CAD) and uses [CadQuery](https://github.com/CadQuery/cadquery) as the CAD scripting framework.

## License

This dataset is released under the [Apache 2.0 License](LICENSE).
