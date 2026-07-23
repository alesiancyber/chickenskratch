"""Capture endpoints: empty sessions leave no litter and report zero lines
(the UI uses that to skip invoking the meeting-capture skill)."""

import pytest
from starlette.testclient import TestClient

from harness.profiles import Config, Profile
from harness.web import build_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr("harness.chats.CHATS_DIR", tmp_path / "chats")
    config = Config(
        default_model="fake",
        vault=str(vault),
        models={"fake": Profile(name="fake", model="fake")},
    )
    app = build_app(config, servers=[], vault=str(vault))
    with TestClient(app, base_url="http://127.0.0.1") as tc:
        yield tc, vault


def test_empty_capture_is_discarded(client):
    tc, vault = client
    path = tc.post("/api/capture/start", json={"title": "Nothing"}).json()["path"]
    assert (vault / path).exists()

    result = tc.post("/api/capture/stop").json()
    assert result == {"path": path, "lines": 0}
    assert not (vault / path).exists()  # note deleted, no litter


def test_capture_with_content_is_kept(client):
    tc, vault = client
    path = tc.post("/api/capture/start", json={"title": "Standup"}).json()["path"]
    tc.post("/api/capture/append", json={"text": "discussed the thing"})
    tc.post("/api/capture/append", json={"text": "sam owns follow-up"})

    result = tc.post("/api/capture/stop").json()
    assert result["lines"] == 2
    content = (vault / path).read_text()
    assert "discussed the thing" in content
    assert "sam owns follow-up" in content


def test_empty_stop_never_deletes_preexisting_note(client):
    import datetime as dt

    tc, vault = client
    (vault / "Meetings").mkdir()
    existing = vault / f"Meetings/{dt.date.today().isoformat()} Old.md"
    existing.write_text("precious notes\n")

    # starting a capture that resolves to the existing note (created=False)
    path = tc.post("/api/capture/start", json={"title": "Old"}).json()["path"]
    assert path.endswith("Old.md")
    tc.post("/api/capture/stop")
    assert existing.exists()  # never deleted: we didn't create it
    assert "precious notes" in existing.read_text()