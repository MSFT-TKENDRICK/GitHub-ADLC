"""Diagnostic: run the pipeline stage by stage with timings, in a temp repo."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from adlc.config import Config
from adlc.reduce import reduce_run
from adlc.runs import RunDir, new_run_id

BRIEF = (
    Path(__file__).resolve().parents[1] / "examples" / "briefs" / "dark-mode.md"
).read_text(encoding="utf-8")


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="adlc-diag-"))
    root = tmp / "consumer"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.email", "a@b.invalid", cwd=root)
    git("config", "user.name", "Diag", cwd=root)
    (root / "README.md").write_text("# Consumer\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def mount():\n    return 'app'\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)

    (root / ".adlc").mkdir()
    (root / ".adlc" / "config.yaml").write_text(
        'version: 1\nprofile: minimal\ncommands:\n  test: "python -c \\"print(1)\\""\n',
        encoding="utf-8",
    )

    os.chdir(root)
    os.environ["ADLC_ROOT"] = str(root)
    os.environ["ADLC_TEST_COMMAND"] = 'python -c "print(1)"'
    cfg = Config.load(root)

    rd = RunDir(cfg, new_run_id())
    rd.create(profile="minimal", brief_text=BRIEF)

    from adlc.stages.build import run_build
    from adlc.stages.enrich import run_enrich
    from adlc.stages.evals import run_eval
    from adlc.stages.evidence import run_evidence
    from adlc.stages.gates import run_gates
    from adlc.stages.graph import run_graph
    from adlc.stages.intake import run_intake, run_qualify
    from adlc.stages.report import run_report
    from adlc.stages.spec import run_spec

    steps = [
        ("intake", lambda: run_intake(cfg, rd, "diag")),
        ("qualify", lambda: run_qualify(cfg, rd)),
        ("spec", lambda: run_spec(cfg, rd)),
        ("enrich", lambda: run_enrich(cfg, rd)),
        ("graph", lambda: run_graph(cfg, rd)),
        ("build", lambda: run_build(cfg, rd, runner_name="fake")),
        ("evidence", lambda: run_evidence(cfg, rd, "candidate-a")),
        ("eval", lambda: run_eval(cfg, rd)),
        ("gates", lambda: run_gates(cfg, rd)),
        ("reduce", lambda: reduce_run(cfg, rd)),
        ("report", lambda: run_report(cfg, rd)),
    ]

    failed = False
    for name, fn in steps:
        started = time.time()
        print(f"--> {name} ...", flush=True)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"    {name} RAISED {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
            failed = True
            break
        print(f"    {name} done in {time.time() - started:.2f}s", flush=True)

    keep = os.environ.get("ADLC_DIAG_KEEP")
    print(f"\nrun dir: {rd.path}")
    if not keep:
        os.chdir(Path(__file__).resolve().parents[1])
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
