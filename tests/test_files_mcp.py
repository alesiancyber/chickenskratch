"""Files MCP server: matching, ranking, skips, path safety, binary skip."""

import pytest

from files_mcp import server


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    (tmp_path / "projects/alpha").mkdir(parents=True)
    (tmp_path / "projects/alpha/models.yaml").write_text("model: gemma\nconfirm_writes: true\n")
    (tmp_path / "projects/alpha/readme.md").write_text("hello alpha\n")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes/models-old.yaml").write_text("model: qwen\n")
    (tmp_path / "node_modules/dep").mkdir(parents=True)
    (tmp_path / "node_modules/dep/models.yaml").write_text("should be skipped\n")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden/models.yaml").write_text("hidden too\n")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01models.yaml\x00")
    monkeypatch.setenv("FILES_ROOTS", str(tmp_path))
    return tmp_path


def test_find_file_ranks_exact_first_and_skips_junk(tree):
    exact_only = server.find_file("models.yaml")
    assert len(exact_only["matches"]) == 1  # exact name doesn't drag in models-old.yaml

    result = server.find_file("models")
    assert [m.rsplit("/", 2)[-2:] for m in result["matches"]] == [
        ["alpha", "models.yaml"],       # exact stem match ranks first
        ["notes", "models-old.yaml"],   # partial after
    ]
    assert not result["truncated"]
    # node_modules and dot-dirs never appear
    assert not any("node_modules" in m or ".hidden" in m for m in result["matches"])


def test_find_file_partial_name(tree):
    result = server.find_file("readme")
    assert len(result["matches"]) == 1
    assert result["matches"][0].endswith("readme.md")


def test_find_file_matches_directories(tree):
    result = server.find_file("alpha")
    assert any(m.endswith("projects/alpha/") for m in result["matches"])  # dir, trailing slash

    partial = server.find_file("proj")
    assert any(m.endswith("projects/") for m in partial["matches"])


def test_search_text_skips_binaries(tree):
    result = server.search_text("models.yaml")
    files = [m["file"] for m in result["matches"]]
    assert not any(f.endswith("blob.bin") for f in files)

    hit = server.search_text("confirm_writes")
    assert hit["matches"][0]["line"] == 2
    assert "confirm_writes: true" in hit["matches"][0]["text"]


def test_root_scoping_and_escape_rejected(tree):
    scoped = server.find_file("models", root="notes")
    assert len(scoped["matches"]) == 1
    assert scoped["matches"][0].endswith("models-old.yaml")

    with pytest.raises(ValueError, match="inside"):
        server.find_file("passwd", root="/etc")


def test_list_dir(tree):
    result = server.list_dir("projects/alpha")
    assert result["dirs"] == []
    assert {f["name"] for f in result["files"]} == {"models.yaml", "readme.md"}
