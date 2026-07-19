import json
from pathlib import Path
from typing import Any

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

_METADATA_FIELDS = (
    "part_type",
    "features_used",
    "modeling_hints",
    "manufacturing_notes",
    "failure_modes",
)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def example_document(example: dict) -> str:
    parts: list[str] = [
        str(example.get("description", "")),
        str(example.get("description_en", "")),
        str(example.get("category", "")),
        str(example.get("part_type", "")),
        " ".join(_as_list(example.get("tags"))),
    ]
    for field in _METADATA_FIELDS:
        parts.extend(_as_list(example.get(field)))
    print_profile = example.get("print_profile")
    if isinstance(print_profile, dict):
        parts.extend(f"{key} {value}" for key, value in print_profile.items())
    return " ".join(part for part in parts if part)


def metadata_boost(
    example: dict,
    *,
    part_type: str | None = None,
    features: list[str] | None = None,
    modeling_hint: str | None = None,
) -> float:
    boost = 0.0
    if part_type and str(example.get("part_type", "")).lower() == part_type.lower():
        boost += 0.25
    if modeling_hint and modeling_hint.lower() in {item.lower() for item in _as_list(example.get("modeling_hints"))}:
        boost += 0.15

    feature_text = " ".join(
        _as_list(example.get("features_used"))
        + _as_list(example.get("tags"))
        + _as_list(example.get("manufacturing_notes"))
    ).lower()
    for feature in features or []:
        tokens = [token for token in str(feature).lower().replace(":", " ").replace("=", " ").split() if len(token) >= 3]
        if tokens and any(token in feature_text for token in tokens):
            boost += 0.08
    return min(boost, 0.45)


def example_result(example: dict, score: float) -> dict:
    return {
        "id": example.get("id", ""),
        "description": example.get("description", ""),
        "description_en": example.get("description_en", ""),
        "code": example.get("code", ""),
        "category": example.get("category", ""),
        "part_type": example.get("part_type", ""),
        "features_used": _as_list(example.get("features_used")),
        "modeling_hints": _as_list(example.get("modeling_hints")),
        "manufacturing_notes": _as_list(example.get("manufacturing_notes")),
        "failure_modes": _as_list(example.get("failure_modes")),
        "print_profile": example.get("print_profile") or {},
        "score": round(float(score), 4),
    }


class ExampleRetriever:
    def __init__(self, examples_dir: str = "examples"):
        self.examples = []
        self.vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3))

        examples_path = Path(examples_dir)
        if not examples_path.is_absolute():
            examples_path = Path(__file__).parent.parent.parent / examples_dir

        for json_file in sorted(examples_path.glob("*.json")):
            with open(json_file, "r", encoding="utf-8") as f:
                example = json.load(f)
                self.examples.append(example)

        if self.examples:
            texts = [example_document(example) for example in self.examples]
            self.tfidf_matrix = self.vectorizer.fit_transform(texts)

    async def find_similar(
        self,
        query: str,
        top_k: int = 3,
        *,
        part_type: str | None = None,
        features: list[str] | None = None,
        modeling_hint: str | None = None,
    ) -> list[dict]:
        if not self.examples:
            return []

        query_vec = self.vectorizer.transform([query])
        similarities = cosine_similarity(query_vec, self.tfidf_matrix).flatten()

        scored = []
        for index, base_score in enumerate(similarities):
            boost = metadata_boost(
                self.examples[index],
                part_type=part_type,
                features=features,
                modeling_hint=modeling_hint,
            )
            score = float(base_score) + boost
            if score > 0.05:
                scored.append((index, score))

        scored.sort(key=lambda item: item[1], reverse=True)
        return [example_result(self.examples[index], score) for index, score in scored[:top_k]]
