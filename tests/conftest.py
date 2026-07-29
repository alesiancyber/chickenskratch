"""Shared fixtures: one way to build a disposable vault, one way to
build an engine with an isolated queue/state. Suites that need extras
(a git baseline, planted notes, scripted drafters) layer them on via
pytest's same-name fixture override."""

import shutil
from pathlib import Path

import pytest

import tezcat_vault.vault as vault_mod
from tezcat.watcher import Watcher
from tezcat_vault.vault import Vault

FIXTURE_VAULT = Path(__file__).resolve().parent / "fixture_vault"


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """A disposable copy of the fixture vault wired as the active vault.
    No git baseline — engine suites use `gitvault`. Event emission is
    re-anchored under tmp_path so test passes never write the repo's
    real data/ events files."""
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, root, ignore=shutil.ignore_patterns(".git"))
    monkeypatch.setenv("TEZCAT_VAULT_PATH", str(root))
    monkeypatch.delenv("HARNESS_CHAT_ID", raising=False)
    monkeypatch.setattr(vault_mod, "_vault", None)
    monkeypatch.setattr("tezcat.events.ROOT", tmp_path)
    monkeypatch.setattr("tezcat.corpus.CORPUS_DIR", tmp_path / "corpus")
    return root


@pytest.fixture()
def gitvault(vault):
    """The vault with a git baseline — what the engine memo diffs against."""
    Vault(vault).commit_if_dirty("baseline")
    return vault


def record_reply(tasks, kind="capture", title="", attendees=(), summary=""):
    """A scripted extract_record reply — the capture lane's decomposition
    contract ({record, tasks})."""
    return {"record": {"kind": kind, "title": title,
                       "attendees": list(attendees), "summary": summary},
            "tasks": [dict(t) for t in tasks]}


def record_worker_returning(tasks, **record_kw):
    """An async record worker that returns the same decomposition for
    any dump — the capture suites' stand-in for the worker LLM."""
    async def worker(text, today):
        return record_reply(tasks, **record_kw)
    return worker


@pytest.fixture()
def make_watcher(tmp_path, monkeypatch):
    """Watcher factory: isolated queue/state, corpus recording off. Kwargs
    pass through (drafters, n_max, registry, disabled)."""
    monkeypatch.setattr("tezcat.watcher.record_dashboard", lambda *a, **k: None)

    def make(vault_path, **kw):
        kw.setdefault("proposals_dir", tmp_path / "proposals")
        kw.setdefault("state_path", tmp_path / "state.json")
        return Watcher(str(vault_path), **kw)

    return make
