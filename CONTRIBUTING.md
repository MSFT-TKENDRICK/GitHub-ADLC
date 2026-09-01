# Contributing to ADLC

ADLC is a Python package plus reusable GitHub workflows. Contributions should
preserve its credential-free default path, explicit opt-in integrations, and
append-only run history.

## Before you change code

1. Read [`docs/PLAN.md`](docs/PLAN.md) for the architecture and frozen
   contracts.
2. Check the relevant guide in [`docs/README.md`](docs/README.md).
3. Confirm the working tree is clean enough to distinguish your changes from
   existing work.

## Development setup

```bash
python -m pip install -e ".[dev]"
python -m pytest tests/conformance -q
ruff check src/
```

Run the smallest relevant test directory while iterating, then run the full
suite before submitting a change. Tests must pass without credentials or
network-only services.

## Design rules

- **Use the frozen ports.** Implement the Protocols in `src/adlc/ports.py`;
  do not change the ports or `schemas/*.json` to fit an adapter.
- **Keep adapters additive.** Optional integrations are registered in the
  existing `pyproject.toml` entry-point groups. Do not add a second discovery
  mechanism.
- **Keep detection cheap.** `detect()` must not raise, make network calls, or
  run a command that can hang. Return `(False, "<specific reason>")` when the
  integration is unavailable.
- **Fail closed.** A required unavailable gate is `not_run` and fails the
  aggregate. Never report `pass` for work that was not verified.
- **Respect protected paths.** Agent-authored patches cannot change
  `.github/**`, `.adlc/**`, `schemas/**`, `docs/decisions/**`, or
  `pyproject.toml`.
- **Preserve immutability.** Stages create new attempt files. Only the reducer
  writes `run.json`; revisions create a new run with `referencesRun`.
- **Keep documentation truthful.** Commands, paths, configuration keys, and
  integration status must match the code. Mark preview APIs and disabled
  examples as such.

## Adding an adapter

Use the existing entry-point group for the seam (`adlc.agents`,
`adlc.evidence`, `adlc.gate`, and so on). A typical adapter looks like:

```python
from pathlib import Path

from adlc.config import Config
from adlc.ports import ArtifactRef, Run


class MyCollector:
    name = "my-collector"
    kind = "evidence"

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        return True, "my-tool available"

    def collect(self, run: Run, variant: str, out: Path) -> list[ArtifactRef]:
        ...
```

Follow the closest existing adapter for the complete Protocol and artifact
contract. Add tests under the matching `tests/` leaf, including the
credentialless unavailable path.

## Documentation changes

Update the nearest focused guide and the documentation index when adding a
public command, adapter, configuration key, schema, workflow, or example. Use
relative links, fenced code blocks with the correct language, and headings that
make the page scannable. Avoid duplicating the full architecture contract in
multiple files; link to `docs/PLAN.md` instead.

## Pull request checklist

- [ ] The change is limited to the relevant package, tests, workflow, or docs.
- [ ] Public behavior and configuration are documented.
- [ ] Optional integrations degrade with a specific `detect()` reason.
- [ ] `python -m pytest tests/` passes.
- [ ] `ruff check src/` passes.
- [ ] New or changed schemas and contracts have focused tests.
- [ ] No credentials, generated run artifacts, or secrets are committed.
