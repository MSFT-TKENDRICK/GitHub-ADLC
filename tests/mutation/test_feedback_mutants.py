"""Source-level mutation smoke tests for high-risk feedback guards.

This is intentionally small and deterministic. Each case copies ``src/adlc`` to a
temporary import root, applies one source mutation, then proves an executable
check kills that mutant. If the check ever starts passing under the mutated
source, the corresponding guard has become untested.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"


@pytest.mark.parametrize(
    ("name", "old", "new", "probe"),
    [
        (
            "surrogate sanitizer removed",
            '_SURROGATE_RE.sub("", _SPOOF_RE.sub("", _CONTROL_RE.sub("", text))).strip()',
            '_SPOOF_RE.sub("", _CONTROL_RE.sub("", text)).strip()',
            (
                "from adlc.stages.feedback import clean_text\n"
                "assert clean_text('a\\ud800b') == 'ab'\n"
            ),
        ),
        (
            "contradictory diff decision guard inverted",
            "if len(decisions) > 1",
            "if len(decisions) < 1",
            (
                "from adlc.stages.feedback import contradictory_decisions\n"
                "pack = {'diffDecisions': [\n"
                "  {'targetKind': 'measurement', 'targetId': 'lcp', 'decision': 'accept'},\n"
                "  {'targetKind': 'measurement', 'targetId': 'lcp', 'decision': 'reject'},\n"
                "]}\n"
                "assert contradictory_decisions(pack) == ['measurement:lcp']\n"
            ),
        ),
    ],
)
def test_feedback_guard_mutants_are_killed(
    tmp_path: Path, name: str, old: str, new: str, probe: str
) -> None:
    temp_src = tmp_path / "src"
    shutil.copytree(SRC_ROOT / "adlc", temp_src / "adlc")
    feedback = temp_src / "adlc" / "stages" / "feedback.py"
    text = feedback.read_text(encoding="utf-8")
    assert old in text, f"mutation point disappeared: {name}"
    feedback.write_text(text.replace(old, new, 1), encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(temp_src)
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0, (
        f"mutant survived: {name}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
