"""Text embeddings for the vault — the substrate auto-linking needs.

Cross-platform by construction: vectors come from an OpenAI-compatible
`/v1/embeddings` endpoint, exactly like every other model in this
project. That works with Ollama (macOS/Linux/Windows), llama.cpp,
LM Studio, vLLM, or a cloud provider, and it adds no model runtime to
this package — the `openai` client is already a dependency. No torch,
no ONNX, no platform-specific accelerator. Point EMBED_BASE_URL and
EMBED_MODEL wherever you like; the harness doesn't care.

The cache is a derived artifact, so it lives in data/ beside the other
per-vault state (proposals, watcher state, events) and is keyed the same
way. Nothing here writes to the vault: embeddings are read-only
telemetry about content, and the vault stays portable markdown that
knows nothing about them.

Invalidation is by content hash, not mtime — the same token the proposal
queue and the editor use. A note whose bytes are unchanged is never
re-embedded, so a full pass over an unchanged vault costs one file read.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

from openai import AsyncOpenAI

from tezcat_vault.vault import atomic_write_text

from .bootstrap import ROOT, vault_digest
from .proposals import content_hash

# nomic-embed-text is the default because it's small, strong on retrieval,
# and `ollama pull nomic-embed-text` works identically on all three
# platforms. Any endpoint serving /v1/embeddings will do.
DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "nomic-embed-text"

# Long notes are truncated rather than chunked in v1: linking works at
# note and block granularity, and a note's opening is overwhelmingly
# what says which note it is. Chunking is a later, separately-evaluated
# layer — the same discipline today.py's docstring sets out.
MAX_CHARS = 8000


def base_url() -> str:
    return os.environ.get("EMBED_BASE_URL", DEFAULT_BASE_URL)


def model() -> str:
    return os.environ.get("EMBED_MODEL", DEFAULT_MODEL)


def cache_path(vault_path: str) -> Path:
    return ROOT / "data" / f"embeddings-{vault_digest(vault_path)}.jsonl"


class EmbeddingError(RuntimeError):
    """The endpoint is unreachable or refused. Callers degrade — linking
    is a convenience, never a stage a pass may fail on (I6)."""


def _client() -> AsyncOpenAI:
    # api_key is required by the client but ignored by local servers.
    return AsyncOpenAI(base_url=base_url(),
                       api_key=os.environ.get("EMBED_API_KEY") or "local")


async def embed(texts: list[str], client=None) -> list[list[float]]:
    """Vectors for `texts`, in order. One request; the endpoint batches."""
    if not texts:
        return []
    client = client or _client()
    try:
        resp = await client.embeddings.create(
            model=model(), input=[t[:MAX_CHARS] or " " for t in texts])
    except Exception as e:
        raise EmbeddingError(
            f"embedding endpoint {base_url()} ({model()}) unavailable: {e}") from None
    # Order is guaranteed by index, not by position in the response.
    return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]


def normalize(vec: list[float]) -> list[float]:
    """Unit-length, so cosine similarity is a plain dot product later —
    the normalization is paid once at write time instead of on every
    comparison."""
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


def cosine(a: list[float], b: list[float]) -> float:
    """Similarity of two NORMALIZED vectors."""
    return sum(x * y for x, y in zip(a, b, strict=False))


class Index:
    """Per-vault note vectors, cached on disk as JSONL.

    JSONL not a vector store: at vault scale — hundreds to a few thousand
    notes — a database buys nothing a dot product doesn't, and it would
    be a dependency to install on three platforms. Rows are
    {key, hash, vec}; a row whose hash still matches the note's bytes is
    reused, everything else is re-embedded.
    """

    def __init__(self, vault_path: str):
        self.path = cache_path(vault_path)
        self.rows: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        self.rows = {}
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn line degrades the cache, never the pass
            if isinstance(row, dict) and row.get("key"):
                self.rows[row["key"]] = row

    def save(self) -> None:
        atomic_write_text(
            self.path,
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self.rows.values()))

    def stale(self, items: dict[str, str]) -> list[str]:
        """Which of {key: text} need embedding — absent, or the bytes moved."""
        return [k for k, text in items.items()
                if self.rows.get(k, {}).get("hash") != content_hash(text)]

    async def update(self, items: dict[str, str], client=None) -> int:
        """Embed whatever is stale and persist. Returns how many were sent.
        Keys that vanished from `items` are dropped, so a deleted note
        doesn't linger as a link candidate forever."""
        todo = self.stale(items)
        if todo:
            vectors = await embed([items[k] for k in todo], client=client)
            for key, vec in zip(todo, vectors, strict=True):
                self.rows[key] = {"key": key, "hash": content_hash(items[key]),
                                  "vec": normalize(vec)}
        for gone in set(self.rows) - set(items):
            del self.rows[gone]
        self.save()
        return len(todo)

    def nearest(self, key: str, limit: int = 5, threshold: float = 0.0,
                within: set[str] | None = None) -> list[tuple[str, float]]:
        """Most similar keys to `key`, nearest first.

        `within` restricts the candidate set, and callers should almost
        always pass it. grouping.py already learned this the hard way:
        semantic similarity alone can't know two clients' notes must
        never mix, but structure can. Build the boundary in code, then
        let similarity rank inside it.
        """
        me = self.rows.get(key)
        if me is None:
            return []
        scored = [
            (k, cosine(me["vec"], row["vec"]))
            for k, row in self.rows.items()
            if k != key and (within is None or k in within)
        ]
        scored = [(k, s) for k, s in scored if s >= threshold]
        scored.sort(key=lambda kv: -kv[1])
        return scored[:limit]
