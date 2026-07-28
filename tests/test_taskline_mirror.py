"""The editor's task-line grammar must stay a MIRROR of tasks.py.

web/src/taskline.js reimplements set_line_scheduled in JavaScript so the
note editor's plan chips can rewrite a line in the open buffer instead of
writing the file underneath it (which would stale the buffer and force a
reload that discards unsaved edits). The engine then re-reads whatever the
editor wrote with the Python regexes — so the two implementations agreeing
is a correctness property, not a nicety, and a silent drift would corrupt
the field order or the trailing block ref.

Run under node: it's the only way to test the real shipped code rather
than a Python paraphrase of it.
"""

import json
import os
import shutil
import subprocess

import pytest

from tezcat.bootstrap import ROOT
from tezcat_vault import tasks as tk

TASKLINE_JS = ROOT / "web" / "src" / "taskline.js"

# The cases that actually distinguish implementations: an empty line, one
# with each later field present (the insert has to land BEFORE them), one
# with a block ref (must stay last), one already carrying ⏳ (replace, not
# append), odd spacing, and the clear.
CASES = [
    ("- [ ] call the vet", "2026-08-02"),
    ("- [ ] call the vet", None),
    ("- [ ] call the vet ➕ 2026-07-01", "2026-08-02"),
    ("- [ ] call the vet 📅 2026-08-09", "2026-08-02"),
    ("- [ ] call the vet 📅 2026-08-09 ➕ 2026-07-01", "2026-08-02"),
    ("- [ ] call the vet ⏳ 2026-07-19", "2026-08-02"),
    ("- [ ] call the vet ⏳ 2026-07-19", None),
    ("- [ ] call the vet ⏳ 2026-07-19 📅 2026-08-09", "2026-08-02"),
    ("- [ ] call the vet ^t-7d2a408e0209", "2026-08-02"),
    ("- [ ] call the vet ⏳ 2026-07-19 ^t-7d2a408e0209", "2026-08-02"),
    ("- [ ] call the vet ⏳ 2026-07-19 ^t-7d2a408e0209", None),
    ("- [ ] call the vet 🔺 📅 2026-08-09 ➕ 2026-07-01 ^t-7d2a", "2026-08-02"),
    ("  - [ ] indented subtask", "2026-08-02"),
    ("- [ ] trailing space   ", "2026-08-02"),
    ("- [ ] ⌛ 2026-07-19 legacy glyph", "2026-08-02"),
    ("- [x] already done ✅ 2026-07-20", "2026-08-02"),
]

# Cases ride an env var, not argv: `node -e` shifts argv, and the layout
# is one more thing to get subtly wrong in a test whose whole job is to
# catch subtle wrongness.
DRIVER = """
import { setLineScheduled } from %s;
const cases = JSON.parse(process.env.TASKLINE_CASES);
console.log(JSON.stringify(cases.map(([line, when]) => setLineScheduled(line, when))));
"""


def _run_js(cases):
    out = subprocess.run(
        ["node", "--input-type=module", "-e", DRIVER % json.dumps(str(TASKLINE_JS))],
        capture_output=True, text=True, check=True, cwd=ROOT,
        env={**os.environ, "TASKLINE_CASES": json.dumps(cases)})
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_set_line_scheduled_matches_python():
    got = _run_js(CASES)
    want = [tk.set_line_scheduled(line, when) for line, when in CASES]
    # per-case so a failure names the line that drifted, not just "lists differ"
    for (line, when), g, w in zip(CASES, got, want, strict=True):
        assert g == w, f"{line!r} + {when!r}: js {g!r} != py {w!r}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_mirror_survives_a_round_trip_through_the_parser():
    """The stronger claim: what the editor writes, the engine can read —
    same plan date, same text, same block ref."""
    (written,) = _run_js([("- [ ] call the vet ➕ 2026-07-01 ^t-7d2a408e0209",
                           "2026-08-02")])
    (task,) = list(tk.iter_tasks(written))
    assert task.scheduled == "2026-08-02"
    assert task.text == "call the vet"
    assert task.block_id == "t-7d2a408e0209"
