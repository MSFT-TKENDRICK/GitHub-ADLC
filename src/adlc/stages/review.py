"""Review stage -- apply a native GitHub PR review as the human decision.

Deliberately no bespoke command protocol. GitHub already has a review primitive
with exactly the right semantics, native permissions, and a commit binding:

    APPROVED           -> ship        -> ADR accepted
    CHANGES_REQUESTED  -> iterate     -> ADR rejected, and a NEW run is created
    COMMENTED          -> annotations appended to the next run's brief

Two safety properties fall out of using the native event:

* **Bound to a SHA.** A review of an older commit is rejected rather than
  silently applied to newer code.
* **History is immutable.** A revision never edits the prior run; it creates a
  new run carrying ``referencesRun``, so the audit trail is append-only.
"""

from __future__ import annotations

from typing import Any

from adlc.config import Config
from adlc.reduce import load_run
from adlc.runs import RunDir, new_run_id, utcnow

_STATE_MAP = {
    "approved": ("ship", "accepted", "none"),
    "changes_requested": ("iterate", "rejected", "inner"),
    "commented": ("rerun", "proposed", "none"),
    "dismissed": ("rerun", "proposed", "none"),
}


def _route(event: dict[str, Any], default: str) -> str:
    labels = {
        label.get("name", "")
        for label in ((event.get("pull_request") or {}).get("labels") or [])
    }
    if "adlc:route-outer" in labels:
        return "outer"
    if "adlc:route-inner" in labels:
        return "inner"
    return default


def apply_review(cfg: Config, rd: RunDir, event: dict[str, Any]) -> dict[str, Any]:
    """Apply a `pull_request_review` webhook payload to a run."""
    started = utcnow()
    review = event.get("review") or {}
    pull_request = event.get("pull_request") or {}

    state = str(review.get("state", "")).lower()
    if state not in _STATE_MAP:
        raise ValueError(f"unsupported review state '{state}'")

    review_sha = review.get("commit_id") or ""
    head_sha = (pull_request.get("head") or {}).get("sha") or ""
    run = load_run(rd)
    recorded_sha = run.get("headSha") or ""

    # Reject a review of a stale commit -- otherwise a decision could be applied
    # to code the reviewer never saw.
    stale = bool(
        review_sha and head_sha and review_sha != head_sha
    ) or bool(review_sha and recorded_sha and review_sha != recorded_sha)

    outcome, adr_status, default_route = _STATE_MAP[state]
    route = _route(event, default_route)
    reviewer = (review.get("user") or {}).get("login", "unknown")
    body = (review.get("body") or "").strip()

    if stale:
        rd.write_stage(
            "review",
            status="fail",
            message=(
                f"review by @{reviewer} targets {review_sha[:8]} but the run records "
                f"{(recorded_sha or head_sha)[:8]} - refusing to apply a stale review"
            ),
            data={"state": state, "reviewSha": review_sha, "recordedSha": recorded_sha,
                  "stale": True},
            started_at=started,
        )
        return {"applied": False, "reason": "stale review", "reviewSha": review_sha}

    from adlc.stages.adr import create_adr, list_adrs, set_status

    adrs = list_adrs(cfg)
    if adrs:
        adr = set_status(cfg, adrs[-1].number, adr_status, review_sha=review_sha)
    else:
        adr = create_adr(
            cfg,
            title=f"Outcome of ADLC run {rd.run_id}",
            context=f"Decision recorded from a native GitHub PR review by @{reviewer}.",
            chosen=outcome,
            justification=body or "no rationale supplied in the review body",
            status=adr_status,
            run_id=rd.run_id,
            review_sha=review_sha,
            decision_makers=f"@{reviewer}",
        )

    decision = {
        "outcome": outcome,
        "rationale": body or f"native PR review: {state}",
        "decidedBy": reviewer,
        "decidedAt": utcnow(),
        "reviewSha": review_sha,
        "adr": adr.number,
    }

    new_run: str | None = None
    if outcome == "iterate":
        new_run = new_run_id()
        successor = RunDir(cfg, new_run)
        brief = rd.brief.read_text(encoding="utf-8") if rd.brief.is_file() else ""
        annotations = "\n".join(
            f"- @{(c.get('user') or {}).get('login', '?')}: {c.get('body', '').strip()}"
            for c in (event.get("comments") or [])
        )
        successor.create(
            profile=cfg.profile,
            brief_text=(
                f"{brief}\n\n---\n\n## Review feedback (run {rd.run_id})\n\n"
                f"@{reviewer} requested changes:\n\n{body or '(no body)'}\n"
                + (f"\n### Inline annotations\n\n{annotations}\n" if annotations else "")
            ),
            references_run=rd.run_id,
            route=route,
        )

    rd.write_stage(
        "review",
        outputs=[str(adr.path.relative_to(cfg.root).as_posix())],
        message=(
            f"@{reviewer} {state} at {review_sha[:8] or 'unknown'} -> "
            f"{outcome}; ADR {adr.number} {adr_status}"
            + (f"; created successor run {new_run} (route={route})" if new_run else "")
        ),
        data={
            "state": state, "outcome": outcome, "route": route,
            "reviewer": reviewer, "reviewSha": review_sha,
            "adr": adr.number, "adrStatus": adr_status,
            "successorRun": new_run, "decision": decision,
        },
        started_at=started,
    )
    return {"applied": True, "decision": decision, "adr": adr.number, "successorRun": new_run}
