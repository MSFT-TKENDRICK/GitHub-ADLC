"""GitHub Spec Kit bridge.

Spec Kit's helper scripts are explicitly non-interactive and support ``--json``,
so CI and agent harnesses can drive them directly without an IDE chat window.
We use that: when Spec Kit is installed we shell out to its scripts; when it is
not, we emit an equivalent minimal ``spec.md`` / ``tasks.md`` pair using the same
conventions (notably the ``[P]`` parallel marker and ``(depends on Tnnn)``
dependency syntax) so the rest of the pipeline is identical either way.

That fallback is what keeps the conformance suite credential-free and offline.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from adlc.config import Config
from adlc.runs import RunDir, utcnow

SPEC_TEMPLATE = """# Feature Specification: {title}

## Overview

{summary}

## User Scenarios

### US1 - Primary flow
As a user, I want {title_lower}, so that the stated outcome is achieved.

**Acceptance criteria**

- **US1-AC1**: The feature is reachable from the documented entry point.
- **US1-AC2**: The primary flow completes without error.
- **US1-AC3**: The change is covered by an automated test.

## Requirements

- **FR-001**: The system MUST implement {title_lower}.
- **FR-002**: The system MUST NOT regress existing behaviour.
- **NFR-001**: The change MUST keep the measured budget in `benchmarks.yaml`.

## Out of Scope

- Anything not required by the acceptance criteria above.

## Source Brief

{brief}
"""

TASKS_TEMPLATE = """# Tasks: {title}

**Format**: `[ID] [P?] [Story] Description`
- **[P]**: can run in parallel (different files, no dependencies)
- **[Story]**: the user story this task serves

## Phase 1: Setup

- [ ] T001 [P] Add implementation module in {impl_path}
- [ ] T002 [P] Add test module in {test_path}

## Phase 2: User Story 1

- [ ] T003 [US1] Wire the feature into the entry point in {wire_path} (depends on T001, T002)

## Dependencies & Execution Order

- T001 and T002 are independent and run in parallel at level 0.
- T003 depends on T001 and T002 and runs at level 1.
"""


def spec_kit_available() -> tuple[bool, str]:
    if shutil.which("specify"):
        return True, "specify CLI on PATH"
    return False, "specify CLI not on PATH - using built-in minimal spec templates"


def _script(root: Path, name: str) -> Path | None:
    """Locate a Spec Kit helper script in a repo that ran `specify init`."""
    for flavour in ("bash", "python", "powershell"):
        for ext in ("sh", "py", "ps1"):
            candidate = root / ".specify" / "scripts" / flavour / f"{name}.{ext}"
            if candidate.is_file():
                return candidate
    return None


def _run_script(path: Path, *args: str, cwd: Path) -> dict[str, Any]:
    if path.suffix == ".py":
        cmd = ["python", str(path), *args]
    elif path.suffix == ".ps1":
        cmd = ["pwsh", "-File", str(path), *args]
    else:
        cmd = ["bash", str(path), *args]
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{path.name} failed: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"raw": proc.stdout.strip()}


def _title_and_summary(brief: str) -> tuple[str, str]:
    lines = [line.strip() for line in brief.splitlines() if line.strip()]
    title = "Untitled change"
    for line in lines:
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            break
        title = line
        break
    body = [line for line in lines if not line.startswith(("#", ">"))]
    summary = body[0] if body else title
    return title, summary


def run_spec(cfg: Config, rd: RunDir) -> dict[str, Any]:
    started = utcnow()
    brief = rd.brief.read_text(encoding="utf-8") if rd.brief.is_file() else ""
    title, summary = _title_and_summary(brief)
    rd.spec_dir.mkdir(parents=True, exist_ok=True)

    available, reason = spec_kit_available()
    used_spec_kit = False
    detail: dict[str, Any] = {"specKit": reason}

    if available and _script(cfg.root, "create-new-feature"):
        script = _script(cfg.root, "create-new-feature")
        if script is not None:
            try:
                detail["createFeature"] = _run_script(
                    script, "--json", "--short-name", rd.run_id, title, cwd=cfg.root
                )
                if setup := _script(cfg.root, "setup-plan"):
                    detail["setupPlan"] = _run_script(setup, "--json", cwd=cfg.root)
                used_spec_kit = True
            except RuntimeError as exc:
                detail["specKitError"] = str(exc)

    spec_path = rd.spec_dir / "spec.md"
    tasks_path = rd.spec_dir / "tasks.md"

    if not spec_path.is_file():
        spec_path.write_text(
            SPEC_TEMPLATE.format(
                title=title, title_lower=title[0].lower() + title[1:] if title else "the change",
                summary=summary, brief=brief.strip(),
            ),
            encoding="utf-8",
        )
    if not tasks_path.is_file():
        slug = "".join(ch if ch.isalnum() else "_" for ch in title.lower())[:32].strip("_") or "feature"
        tasks_path.write_text(
            TASKS_TEMPLATE.format(
                title=title,
                impl_path=f"src/{slug}.py",
                test_path=f"tests/test_{slug}.py",
                wire_path=f"src/{slug}_entry.py",
            ),
            encoding="utf-8",
        )

    outputs = [rd.rel(spec_path), rd.rel(tasks_path)]
    rd.write_stage(
        "spec",
        outputs=outputs,
        message=("spec-kit driven" if used_spec_kit else f"built-in templates ({reason})"),
        data={"usedSpecKit": used_spec_kit, "title": title, **detail},
        started_at=started,
    )
    return {"title": title, "usedSpecKit": used_spec_kit, "outputs": outputs}
