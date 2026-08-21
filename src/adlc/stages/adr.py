"""Architecture Decision Records in MADR v4 format.

ADRs are the auditable, permanent record of *why*. They live in
``docs/decisions/`` (git-tracked, never inside a run directory) and are bound to
the commit SHA of the review that decided them, so a decision cannot silently
drift from the code it was made about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from adlc.config import Config

STATUSES = ("proposed", "accepted", "rejected", "deprecated", "superseded")

MADR_TEMPLATE = """---
status: {status}
date: {date}
decision-makers: {decision_makers}
consulted: {consulted}
informed: {informed}
adlc-run: {run_id}
adlc-review-sha: {review_sha}
---

# {title}

## Context and Problem Statement

{context}

## Decision Drivers

{drivers}

## Considered Options

{options}

## Decision Outcome

Chosen option: "{chosen}", because {justification}

### Consequences

{consequences}

### Confirmation

{confirmation}

## More Information

{more_info}
"""


@dataclass
class Adr:
    number: str
    path: Path
    title: str
    status: str

    @property
    def slug(self) -> str:
        return self.path.stem


def _next_number(directory: Path) -> str:
    highest = 0
    for path in directory.glob("[0-9][0-9][0-9][0-9]-*.md"):
        try:
            highest = max(highest, int(path.name[:4]))
        except ValueError:
            continue
    return f"{highest + 1:04d}"


def _slugify(title: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", title.lower())).strip("-")[:60] or "decision"


def create_adr(
    cfg: Config,
    title: str,
    *,
    context: str = "",
    drivers: list[str] | None = None,
    options: list[str] | None = None,
    chosen: str = "",
    justification: str = "",
    consequences: list[str] | None = None,
    confirmation: str = "",
    status: str = "proposed",
    run_id: str = "",
    review_sha: str = "",
    decision_makers: str = "",
) -> Adr:
    directory = cfg.decisions_dir
    directory.mkdir(parents=True, exist_ok=True)
    number = _next_number(directory)
    path = directory / f"{number}-{_slugify(title)}.md"

    def bullets(items: list[str] | None, empty: str) -> str:
        return "\n".join(f"* {item}" for item in items) if items else empty

    body = MADR_TEMPLATE.format(
        status=status,
        date=datetime.now(UTC).strftime("%Y-%m-%d"),
        decision_makers=decision_makers or "ADLC",
        consulted="adversarial review squad, evidence review squad",
        informed="repository maintainers",
        run_id=run_id or "n/a",
        review_sha=review_sha or "n/a",
        title=title,
        context=context or "_To be completed._",
        drivers=bullets(drivers, "* _To be completed._"),
        options=bullets(options, "* _To be completed._"),
        chosen=chosen or title,
        justification=justification or "_to be completed_.",
        consequences=bullets(consequences, "* _To be completed._"),
        confirmation=confirmation or "Confirmed by the ADLC gate results recorded in `run.json`.",
        more_info=(
            f"Produced by ADLC run `{run_id}`. Evidence and gate results are in "
            f"`.adlc/runs/{run_id}/` and summarised in `report.html`."
            if run_id else "_None._"
        ),
    )
    # Encode before the write. ``write_text`` opens (and truncates) the path
    # first and only then encodes, so any UnicodeEncodeError -- a lone surrogate
    # smuggled through a free-text field -- would leave this git-tracked, permanent
    # record at zero bytes. Encoding up front raises before the file is touched.
    path.write_bytes(body.encode("utf-8"))
    return Adr(number=number, path=path, title=title, status=status)


def list_adrs(cfg: Config) -> list[Adr]:
    directory = cfg.decisions_dir
    if not directory.is_dir():
        return []
    found: list[Adr] = []
    for path in sorted(directory.glob("[0-9][0-9][0-9][0-9]-*.md")):
        text = path.read_text(encoding="utf-8")
        status_match = re.search(r"^status:\s*(\S+)", text, re.MULTILINE)
        title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        found.append(Adr(
            number=path.name[:4],
            path=path,
            title=title_match.group(1).strip() if title_match else path.stem,
            status=status_match.group(1).strip() if status_match else "unknown",
        ))
    return found


def set_status(cfg: Config, number: str, status: str, *, review_sha: str = "") -> Adr:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got '{status}'")
    matches = [adr for adr in list_adrs(cfg) if adr.number == number.zfill(4)]
    if not matches:
        raise FileNotFoundError(f"no ADR numbered {number} in {cfg.decisions_dir}")

    adr = matches[0]
    text = adr.path.read_text(encoding="utf-8")
    text = re.sub(r"^status:\s*\S+", f"status: {status}", text, count=1, flags=re.MULTILINE)
    if review_sha:
        if re.search(r"^adlc-review-sha:", text, re.MULTILINE):
            text = re.sub(
                r"^adlc-review-sha:.*$", f"adlc-review-sha: {review_sha}", text, count=1, flags=re.MULTILINE
            )
        else:
            text = text.replace("---\n\n#", f"adlc-review-sha: {review_sha}\n---\n\n#", 1)
    # Same reason as create_adr: encode before the write so a codec failure can
    # never truncate an existing ADR to zero bytes on its way to raising.
    adr.path.write_bytes(text.encode("utf-8"))
    adr.status = status
    return adr
