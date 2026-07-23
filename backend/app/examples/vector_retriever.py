import json
import logging
from pathlib import Path

import chromadb

from app.examples.retriever import example_document, example_result, metadata_boost

logger = logging.getLogger(__name__)

_LIST_METADATA_FIELDS = (
    "tags",
    "features_used",
    "modeling_hints",
    "manufacturing_notes",
    "failure_modes",
)


def _pack_list(value) -> str:
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return "[]"
    return json.dumps([str(value)], ensure_ascii=False)


def _unpack_list(value) -> list[str]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except Exception:
        return [str(value)]
    if isinstance(decoded, list):
        return [str(item) for item in decoded]
    return [str(decoded)]


def _pack_profile(value) -> str:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return "{}"


def _unpack_profile(value) -> dict:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _metadata_from_example(example: dict) -> dict:
    metadata = {
        "id": example.get("id", ""),
        "category": example.get("category", ""),
        "difficulty": example.get("difficulty", ""),
        "code": example.get("code", ""),
        "description": example.get("description", ""),
        "description_en": example.get("description_en", ""),
        "part_type": example.get("part_type", ""),
        "print_profile": _pack_profile(example.get("print_profile")),
    }
    for field in _LIST_METADATA_FIELDS:
        metadata[field] = _pack_list(example.get(field))
    return metadata


def _example_from_metadata(metadata: dict) -> dict:
    example = {
        "id": metadata.get("id", ""),
        "category": metadata.get("category", ""),
        "difficulty": metadata.get("difficulty", ""),
        "code": metadata.get("code", ""),
        "description": metadata.get("description", ""),
        "description_en": metadata.get("description_en", ""),
        "part_type": metadata.get("part_type", ""),
        "print_profile": _unpack_profile(metadata.get("print_profile", "{}")),
    }
    for field in _LIST_METADATA_FIELDS:
        example[field] = _unpack_list(metadata.get(field, "[]"))
    return example


class VectorExampleRetriever:
    def __init__(self, examples_dir: str = "examples", persist_dir: str = "./data/chroma"):
        examples_path = Path(examples_dir)
        if not examples_path.is_absolute():
            examples_path = Path(__file__).parent.parent.parent / examples_dir

        persist_path = Path(persist_dir)
        if not persist_path.is_absolute():
            persist_path = Path(__file__).parent.parent.parent / persist_dir

        persist_path.mkdir(parents=True, exist_ok=True)

        self.chroma = chromadb.PersistentClient(path=str(persist_path))
        self.collection = self.chroma.get_or_create_collection(
            name="cad_examples_v2",
            metadata={"hnsw:space": "cosine"},
        )

        examples = []
        for json_file in sorted(examples_path.glob("*.json")):
            with open(json_file, "r", encoding="utf-8") as f:
                examples.append(json.load(f))

        ids = []
        docs = []
        metas = []
        for example in examples:
            example_id = example.get("id", example.get("description", "")[:50])
            ids.append(example_id)
            docs.append(example_document(example))
            metas.append(_metadata_from_example(example))

        if ids:
            self.collection.upsert(ids=ids, documents=docs, metadatas=metas)
            logger.info(f"Indexed/updated {len(ids)} examples into ChromaDB")

    async def find_similar(
        self,
        query: str,
        top_k: int = 3,
        *,
        part_type: str | None = None,
        features: list[str] | None = None,
        modeling_hint: str | None = None,
    ) -> list[dict]:
        if self.collection.count() == 0:
            return []

        n_results = min(max(top_k * 3, top_k), self.collection.count())
        results = self.collection.query(query_texts=[query], n_results=n_results)

        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        output = []
        for metadata, distance in zip(metadatas, distances):
            example = _example_from_metadata(metadata)
            score = (1 - float(distance)) + metadata_boost(
                example,
                part_type=part_type,
                features=features,
                modeling_hint=modeling_hint,
            )
            output.append(example_result(example, score))

        output.sort(key=lambda item: item["score"], reverse=True)
        return output[:top_k]
