import json
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


class ExampleRetriever:
    def __init__(self, examples_dir: str = "examples"):
        self.examples = []
        self.vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3))

        examples_path = Path(examples_dir)
        if not examples_path.is_absolute():
            # Resolve relative to the backend directory
            examples_path = Path(__file__).parent.parent.parent / examples_dir

        for json_file in sorted(examples_path.glob("*.json")):
            with open(json_file, "r", encoding="utf-8") as f:
                example = json.load(f)
                self.examples.append(example)

        if self.examples:
            texts = [
                f"{ex.get('description', '')} {ex.get('description_en', '')} {' '.join(ex.get('tags', []))}"
                for ex in self.examples
            ]
            self.tfidf_matrix = self.vectorizer.fit_transform(texts)

    async def find_similar(self, query: str, top_k: int = 3) -> list[dict]:
        if not self.examples:
            return []

        query_vec = self.vectorizer.transform([query])
        similarities = cosine_similarity(query_vec, self.tfidf_matrix).flatten()

        scored = []
        for i, score in enumerate(similarities):
            if score > 0.05:
                scored.append((i, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        scored = scored[:top_k]

        results = []
        for idx, score in scored:
            ex = self.examples[idx]
            results.append({
                "description": ex.get("description", ""),
                "code": ex.get("code", ""),
                "category": ex.get("category", ""),
                "score": round(float(score), 4),
            })

        return results
