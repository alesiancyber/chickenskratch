"""The embedding cache: correctness of reuse, invalidation, and ranking.

No endpoint is contacted — a fake client stands in, same philosophy as
FakeOpenAI. What's pinned here is that unchanged bytes are never
re-embedded, that changed ones are, and that the candidate boundary is
honoured (grouping.py's lesson: similarity must not cross an entity
boundary on its own).
"""

import asyncio
from types import SimpleNamespace

import pytest

from tezcat import embeddings as em


class FakeEmbedder:
    """Deterministic vectors: each text maps to a point on a circle by its
    first character, so similarity is predictable without a model."""

    def __init__(self):
        self.calls = []
        self.embeddings = SimpleNamespace(create=self._create)

    async def _create(self, model, input):
        self.calls.append(list(input))
        data = []
        for i, text in enumerate(input):
            angle = (ord(text.strip()[:1] or "a") % 12) * 0.5236
            import math
            data.append(SimpleNamespace(
                index=i, embedding=[math.cos(angle), math.sin(angle), 0.1]))
        return SimpleNamespace(data=list(reversed(data)))  # out of order on purpose


@pytest.fixture()
def index(tmp_path, monkeypatch):
    monkeypatch.setattr("tezcat.embeddings.ROOT", tmp_path)
    return em.Index("/some/vault")


def test_results_are_ordered_by_index_not_response_position():
    """Endpoints may return data out of order; only `index` is binding."""
    fake = FakeEmbedder()
    vecs = asyncio.run(em.embed(["alpha", "beta"], client=fake))
    assert len(vecs) == 2
    assert vecs[0] != vecs[1]
    # 'a' and 'b' map to different angles; first vector must be alpha's
    import math
    assert vecs[0][0] == pytest.approx(math.cos((ord("a") % 12) * 0.5236))


def test_unchanged_notes_are_never_re_embedded(index):
    fake = FakeEmbedder()
    items = {"a.md": "alpha text", "b.md": "beta text"}
    assert asyncio.run(index.update(items, client=fake)) == 2
    assert asyncio.run(index.update(items, client=fake)) == 0   # nothing sent
    assert len(fake.calls) == 1


def test_changed_bytes_invalidate_only_that_note(index):
    fake = FakeEmbedder()
    asyncio.run(index.update({"a.md": "alpha", "b.md": "beta"}, client=fake))
    sent = asyncio.run(index.update({"a.md": "changed", "b.md": "beta"}, client=fake))
    assert sent == 1
    assert fake.calls[-1] == ["changed"]


def test_deleted_notes_leave_the_index(index):
    fake = FakeEmbedder()
    asyncio.run(index.update({"a.md": "alpha", "b.md": "beta"}, client=fake))
    asyncio.run(index.update({"a.md": "alpha"}, client=fake))
    assert set(index.rows) == {"a.md"}


def test_cache_survives_a_reload(index, tmp_path, monkeypatch):
    fake = FakeEmbedder()
    asyncio.run(index.update({"a.md": "alpha"}, client=fake))
    fresh = em.Index("/some/vault")
    assert set(fresh.rows) == {"a.md"}
    assert asyncio.run(fresh.update({"a.md": "alpha"}, client=fake)) == 0


def test_a_torn_cache_line_degrades_rather_than_crashes(index):
    fake = FakeEmbedder()
    asyncio.run(index.update({"a.md": "alpha", "b.md": "beta"}, client=fake))
    index.path.write_text(index.path.read_text() + '{"key": "c.md", "vec"\n')
    fresh = em.Index("/some/vault")
    assert set(fresh.rows) == {"a.md", "b.md"}


def test_nearest_respects_the_candidate_boundary(index):
    """grouping.py's lesson, enforced: similarity ranks WITHIN a boundary
    built in code — it never gets to cross one on its own."""
    fake = FakeEmbedder()
    asyncio.run(index.update(
        {"client-a/x.md": "alpha", "client-a/y.md": "alpha too",
         "client-b/z.md": "alpha as well"}, client=fake))
    unbounded = [k for k, _ in index.nearest("client-a/x.md")]
    assert "client-b/z.md" in unbounded          # similarity alone would link them
    bounded = index.nearest("client-a/x.md",
                            within={"client-a/y.md"})
    assert [k for k, _ in bounded] == ["client-a/y.md"]


def test_nearest_applies_the_threshold_and_limit(index):
    fake = FakeEmbedder()
    asyncio.run(index.update({f"n{i}.md": c for i, c in enumerate("abcdef")},
                             client=fake))
    assert len(index.nearest("n0.md", limit=2)) == 2
    assert index.nearest("n0.md", threshold=1.01) == []


def test_unreachable_endpoint_raises_a_typed_error(index):
    class Dead:
        embeddings = SimpleNamespace(
            create=lambda **kw: (_ for _ in ()).throw(OSError("refused")))

    with pytest.raises(em.EmbeddingError, match="unavailable"):
        asyncio.run(em.embed(["x"], client=Dead()))
