# Contributing to ADLC — workstream rules

This repo is built by **one spine + ten independent leaves** working in parallel.
These rules are what make that safe. Read them before writing any code.

## The golden rules

1. **Stay inside your exclusive paths.** They are listed in `docs/PLAN.md` §6.
   Do not create or edit files outside them. If you think you need to, you are
   wrong about the contract — re-read `src/adlc/ports.py`.

2. **Never edit `pyproject.toml`.** Every adapter — including yours — is
   *already* registered there. `adlc.config.load_adapters` swallows
   `ImportError`, so an entry point whose module does not exist yet is simply
   undiscoverable, not fatal. Just create the module at the declared path with
   the declared class name.

3. **Never edit `src/adlc/ports.py` or `schemas/*.json`.** They are frozen.
   Code against them.

4. **Your adapter must be a pure addition.** The spine ships a credential-free
   default for every seam. If your adapter is unavailable, `detect()` returns
   `(False, "<specific human-readable reason>")` and the framework carries on.
   Your adapter failing must never fail the spine's conformance suite.

5. **`detect()` must be cheap and must not raise.** No network calls, no
   subprocess that can hang. Check for env vars, importable modules, or a binary
   on `PATH`. Return a *specific* reason string — it is surfaced verbatim to
   users in `capabilities.json` and in any `not_run` gate.

6. **Fail closed, never fail silently.** A required gate that cannot run returns
   `status: "not_run"`, and the aggregator turns that into a build failure.
   Never return `pass` for something you did not actually verify.

## Adapter skeleton

```python
from __future__ import annotations

import shutil
from pathlib import Path

from adlc.config import Config
from adlc.ports import ArtifactRef, Run


class MyCollector:
    name = "my-collector"
    kind = "evidence"

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        if shutil.which("my-tool") is None:
            return False, "my-tool not on PATH"
        return True, "my-tool available"

    def collect(self, run: Run, variant: str, out: Path) -> list[ArtifactRef]:
        ...
```

## Gate skeleton

```python
class MyGate:
    id = "my_gate"
    name = "my-gate"
    kind = "gate"
    required_by_default = False

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]: ...

    def evaluate(self, run: Run, cfg: Config) -> GateResult:
        return {
            "id": self.id,
            "required": cfg.is_required(self.id),
            "status": "pass",          # pass | fail | not_run
            "severity": "medium",
            "observed": {...},
            "expected": {...},
            "message": "human-readable, specific",
            "evidence": ["gates/my_gate.json"],
        }
```

## Tests

Put yours in `tests/<your-leaf>/`. They must pass with **no credentials** — that
means your test asserts the `detect() == (False, reason)` path and, when the tool
*is* present, the happy path. Never write a test that requires a secret to pass.

## Definition of done

- [ ] Module exists at the path already declared in `pyproject.toml`
- [ ] Implements the Protocol from `adlc.ports` exactly
- [ ] `detect()` is cheap, non-raising, and returns a specific reason
- [ ] Unavailable path degrades to `not_run` / skip, never a crash
- [ ] Tests pass with no credentials
- [ ] `python -m pytest tests/` and `ruff check src/` are clean
- [ ] Nothing outside your exclusive paths was touched
