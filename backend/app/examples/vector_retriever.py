import json
import logging
from pathlib import Path

import chromadb

logger = logging.getLogger(__name__)


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
            name="cad_examples",
            metadata={"hnsw:space": "cosine"},
        )

        # Load and index examples
        examples = []
        for json_file in sorted(examples_path.glob("*.json")):
            with open(json_file, "r", encoding="utf-8") as f:
                examples.append(json.load(f))

        # Check which examples need indexing
        existing_ids = set()
        if self.collection.count() > 0:
            all_data = self.collection.get()
            existing_ids = set(all_data["ids"])

        new_ids = []
        new_docs = []
        new_metas = []
        for ex in examples:
            ex_id = ex.get("id", ex.get("description", "")[:50])
            if ex_id not in existing_ids:
                document = (
                    f"{ex.get('description', '')} "
                    f"{ex.get('description_en', '')} "
                    f"{' '.join(ex.get('tags', []))}"
                )
                metadata = {
                    "category": ex.get("category", ""),
                    "difficulty": ex.get("difficulty", ""),
                    "code": ex.get("code", ""),
                    "description": ex.get("description", ""),
                }
                new_ids.append(ex_id)
                new_docs.append(document)
                new_metas.append(metadata)

        if new_ids:
            self.collection.add(
                ids=new_ids,
                documents=new_docs,
                metadatas=new_metas,
            )
            logger.info(f"Indexed {len(new_ids)} new examples into ChromaDB")

    async def find_similar(self, query: str, top_k: int = 3) -> list[dict]:
        if self.collection.count() == 0:
            return []

        results = self.collection.query(
            query_texts=[query],
            n_results=min(top_k, self.collection.count()),
        )

        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        output = []
        for meta, dist in zip(metadatas, distances):
            output.append({
                "description": meta.get("description", ""),
                "code": meta.get("code", ""),
                "category": meta.get("category", ""),
                "score": round(1 - dist, 4),
            })

        return output
