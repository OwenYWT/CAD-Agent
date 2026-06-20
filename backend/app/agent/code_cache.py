"""Content-addressed LRU cache: prompt -> generated CadQuery code.

Skips the 2 LLM calls (plan + codegen) when an identical prompt is generated again.
Execution still runs fresh (so files/validation are regenerated per request) — only
the expensive, deterministic-enough LLM step is cached. Bounded LRU; in-memory only.
"""
import hashlib
from collections import OrderedDict


class CodeCache:
    def __init__(self, max_entries: int = 256):
        self.max_entries = max_entries
        self._store: "OrderedDict[str, str]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(prompt: str) -> str:
        return hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()

    def get(self, prompt: str) -> str | None:
        k = self._key(prompt)
        if k in self._store:
            self._store.move_to_end(k)
            self.hits += 1
            return self._store[k]
        self.misses += 1
        return None

    def put(self, prompt: str, code: str) -> None:
        k = self._key(prompt)
        self._store[k] = code
        self._store.move_to_end(k)
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()
        self.hits = 0
        self.misses = 0
