"""ADLC command line interface.

Every command is idempotent, supports ``--json``, and exits non-zero when a
required gate fails -- so the same binary drives a developer's laptop and a CI
job with no wrapper scripts.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from adlc import __version__
from adlc.config import Config, capabilities
from adlc.reduce import aggregate_passed, load_run, reduce_run
from adlc.runs import RunDir, new_run_id, resolve_run, write_json
from adlc.schemas import is_valid

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="ADLC - governed, evidence-producing agentic SDLC for any repository.",
)
run_app = typer.Typer(no_args_is_help=True, help="Create and inspect runs.")
adr_app = typer.Typer(no_args_is_help=True, help="Architecture decision records.")
review_app = typer.Typer(no_args_is_help=True, help="Apply native PR review decisions.")
export_app = typer.Typer(no_args_is_help=True, help="Export a run to another format.")
app.add_typer(run_app, name="run")
app.add_typer(adr_app, name="adr")
app.add_typer(review_app, name="review")
app.add_typer(export_app, name="export")


def _cfg() -> Config:
    return Config.load()


def _emit(payload: Any, as_json: bool, human: str = "") -> None:
    if as_json:
        typer.echo(json.dumps(payload, indent=2, default=str))
    elif human:
        typer.echo(human)


def _rd(cfg: Config, run_id: str | None) -> RunDir:
    try:
        return resolve_run(cfg, run_id)
    except FileNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc


@app.callback(invoke_without_command=False)
def _root(
    version: bool = typer.Option(False, "--version", help="Show the version and exit."),
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit(0)


# ---------------------------------------------------------------------------
# doctor / init
# ---------------------------------------------------------------------------


@app.command()
def doctor(as_json: bool = typer.Option(False, "--json")) -> None:
    """Probe every adapter and record what is available."""
    cfg = _cfg()
    caps = capabilities(cfg)
    cfg.adlc_dir.mkdir(parents=True, exist_ok=True)
    write_json(cfg.adlc_dir / "capabilities.json", caps)

    if as_json:
        _emit(caps, True)
        return

    typer.echo(f"ADLC {__version__}  profile={cfg.profile}  root={cfg.root}")
    for kind, adapters in caps["kinds"].items():
        if not adapters:
            continue
        typer.echo(f"\n{kind}:")
        for name, info in sorted(adapters.items()):
            mark = typer.style("ready", fg=typer.colors.GREEN) if info["available"] \
                else typer.style("n/a  ", fg=typer.colors.YELLOW)
            typer.echo(f"  {mark}  {name:<22} {info['reason']}")
    typer.echo("\nselected: " + ", ".join(f"{k}={v}" for k, v in caps["selected"].items()))
    typer.echo(f"required gates ({cfg.profile}): {', '.join(cfg.required_gates())}")


@app.command()
def init(
    target: Path = typer.Option(Path.cwd(), "--target", help="Repository to install into."),
    profile: str = typer.Option("minimal", "--profile"),
    ref: str = typer.Option("v0", "--ref", help="Tag/SHA to pin the reusable workflow to."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing ADLC files."),
) -> None:
    """Vendor ADLC into a repository: one pinned caller workflow plus config.

    Deliberately minimal -- it never touches existing CI and never copies the
    framework itself, so upgrading is changing one pinned ref.
    """
    from adlc.templates_data import CALLER_WORKFLOW, CONFIG_YAML, POLICY_YAML, SQUADS_YAML

    target = target.resolve()
    written: list[str] = []
    skipped: list[str] = []

    files = {
        Path(".adlc/config.yaml"): CONFIG_YAML.format(profile=profile, version=__version__),
        Path(".adlc/policy.yaml"): POLICY_YAML,
        Path(".adlc/squads.yaml"): SQUADS_YAML,
        Path(".github/workflows/adlc.yml"): CALLER_WORKFLOW.format(ref=ref),
    }
    for rel, content in files.items():
        path = target / rel
        if path.exists() and not force:
            skipped.append(str(rel))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(str(rel))

    gitignore = target / ".gitignore"
    marker = ".adlc/runs/"
    if not gitignore.exists() or marker not in gitignore.read_text(encoding="utf-8"):
        with gitignore.open("a", encoding="utf-8") as handle:
            handle.write(f"\n# ADLC run artifacts (evidence is uploaded as CI artifacts)\n{marker}\n")
        written.append(".gitignore")

    typer.secho(f"ADLC {__version__} installed into {target}", fg=typer.colors.GREEN)
    for item in written:
        typer.echo(f"  + {item}")
    for item in skipped:
        typer.echo(f"  = {item} (exists; use --force to overwrite)")
    typer.echo("\nNext: `adlc doctor`, then `adlc run new --brief <file>`.")


# ---------------------------------------------------------------------------
# run lifecycle
# ---------------------------------------------------------------------------


@run_app.command("new")
def run_new(
    brief: Path | None = typer.Option(None, "--brief", help="Markdown brief file."),
    issue: int | None = typer.Option(None, "--issue", help="GitHub issue number."),
    profile: str | None = typer.Option(None, "--profile"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Create a run from a brief file or a GitHub issue."""
    from adlc.stages.intake import brief_from_issue, run_intake

    cfg = _cfg()
    if brief is None and issue is None:
        typer.secho("provide --brief or --issue", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)

    if brief is not None:
        text, source = brief.read_text(encoding="utf-8"), str(brief)
    else:
        text, source = brief_from_issue(int(issue)), f"issue#{issue}"

    rd = RunDir(cfg, new_run_id())
    rd.create(profile=profile or cfg.profile, brief_text=text)
    run_intake(cfg, rd, source)
    reduce_run(cfg, rd)

    _emit({"runId": rd.run_id, "path": str(rd.path), "source": source}, as_json,
          f"created run {rd.run_id} at {rd.path}")


@run_app.command("list")
def run_list(as_json: bool = typer.Option(False, "--json")) -> None:
    """List runs, newest last."""
    cfg = _cfg()
    if not cfg.runs_dir.is_dir():
        _emit([], as_json, "no runs yet")
        return
    rows = []
    for directory in sorted(d for d in cfg.runs_dir.iterdir() if d.is_dir()):
        rd = RunDir(cfg, directory.name)
        try:
            run = load_run(rd)
            passed, _ = aggregate_passed(run.get("gates") or [])
            rows.append({"runId": rd.run_id, "status": run.get("status"),
                         "gatesPassed": passed, "created": run.get("createdAt")})
        except FileNotFoundError:
            rows.append({"runId": rd.run_id, "status": "unknown"})
    _emit(rows, as_json, "\n".join(
        f"{r['runId']}  {r.get('status')!s:<10} gates={'pass' if r.get('gatesPassed') else 'fail'}"
        for r in rows
    ))


def _stage_command(name: str, func: Callable[[Config, RunDir], Any]) -> None:
    @app.command(name)
    def _cmd(
        run_id: str = typer.Argument("latest"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        cfg = _cfg()
        rd = _rd(cfg, run_id)
        result = func(cfg, rd)
        reduce_run(cfg, rd)
        latest = rd.latest_stage(name if name != "eval" else "eval")
        message = (latest or {}).get("message", "")
        _emit(result, as_json, f"{name}: {message}")
        if latest and latest.get("status") == "fail":
            raise typer.Exit(1)

    _cmd.__doc__ = func.__doc__ or f"Run the {name} stage."


def _register_simple_stages() -> None:
    from adlc.stages.enrich import run_enrich
    from adlc.stages.graph import run_graph
    from adlc.stages.intake import run_qualify
    from adlc.stages.spec import run_spec

    _stage_command("qualify", run_qualify)
    _stage_command("spec", run_spec)
    _stage_command("enrich", run_enrich)
    _stage_command("graph", run_graph)


_register_simple_stages()


@app.command()
def build(
    run_id: str = typer.Argument("latest"),
    runner: str | None = typer.Option(None, "--runner", help="Agent runner adapter."),
    max_parallel: int | None = typer.Option(None, "--max-parallel"),
    no_resume: bool = typer.Option(False, "--no-resume", help="Ignore completed barriers."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Execute the task graph with parallel levels and patch barriers."""
    from adlc.stages.build import run_build

    cfg = _cfg()
    rd = _rd(cfg, run_id)
    result = run_build(cfg, rd, runner_name=runner, max_parallel=max_parallel,
                       resume=not no_resume)
    reduce_run(cfg, rd)
    latest = rd.latest_stage("build")
    _emit(result, as_json, f"build: {(latest or {}).get('message', '')}")
    if latest and latest.get("status") == "fail":
        raise typer.Exit(1)


@app.command()
def evidence(
    run_id: str = typer.Argument("latest"),
    variant: str = typer.Option("candidate-a", "--variant"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Capture evidence and build the sanitised review pack."""
    from adlc.stages.evidence import run_evidence

    cfg = _cfg()
    rd = _rd(cfg, run_id)
    result = run_evidence(cfg, rd, variant)
    reduce_run(cfg, rd)
    latest = rd.latest_stage("evidence")
    _emit({"artifacts": len(result["artifacts"]), "valid": result["valid"]}, as_json,
          f"evidence: {(latest or {}).get('message', '')}")
    if latest and latest.get("status") == "fail":
        raise typer.Exit(1)


@app.command("eval")
def eval_cmd(
    run_id: str = typer.Argument("latest"),
    runner: str | None = typer.Option(None, "--runner"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Score the candidate against the rubric."""
    from adlc.stages.evals import run_eval

    cfg = _cfg()
    rd = _rd(cfg, run_id)
    score = run_eval(cfg, rd, runner_name=runner)
    reduce_run(cfg, rd)
    _emit(score, as_json,
          f"eval: overall={score.get('overall')} threshold={score.get('threshold')} "
          f"passed={score.get('passed')}")


@app.command()
def gate(
    run_id: str = typer.Argument("latest"),
    ids: str | None = typer.Option(None, "--ids", help="Comma-separated gate ids."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Run gates and enforce the fail-closed aggregate."""
    from adlc.stages.gates import run_gates

    cfg = _cfg()
    rd = _rd(cfg, run_id)
    gate_ids = [g.strip() for g in ids.split(",") if g.strip()] if ids else None
    result = run_gates(cfg, rd, gate_ids)
    reduce_run(cfg, rd)

    if as_json:
        _emit(result, True)
    else:
        for g in result["gates"]:
            colour = {"pass": typer.colors.GREEN, "fail": typer.colors.RED}.get(
                g["status"], typer.colors.YELLOW
            )
            flag = "required" if g["required"] else "optional"
            status_text = typer.style(f"{g['status']:<8}", fg=colour)
            typer.echo(f"  {status_text} {g['id']:<24} [{flag}] {g.get('message', '')}")
        verdict = "PASS" if result["passed"] else "FAIL"
        typer.secho(
            f"\naggregate: {verdict}",
            fg=typer.colors.GREEN if result["passed"] else typer.colors.RED,
        )
    if not result["passed"]:
        raise typer.Exit(1)


@app.command()
def reduce(
    run_id: str = typer.Argument("latest"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Fold immutable stage results into run.json. The only writer."""
    cfg = _cfg()
    rd = _rd(cfg, run_id)
    run = reduce_run(cfg, rd)
    _emit(run, as_json,
          f"reduced {rd.run_id}: {len(run['stages'])} stage result(s), "
          f"{len(run['gates'])} gate(s), {len(run['artifacts'])} artifact(s)")


@app.command()
def report(
    run_id: str = typer.Argument("latest"),
    open_browser: bool = typer.Option(False, "--open"),
    cdn_diagrams: bool = typer.Option(
        False,
        "--cdn-diagrams",
        help=(
            "Render diagrams with Mermaid from a CDN. Off by default: the report is "
            "an evidence artifact and should stay readable without a third party "
            "being online and unmodified."
        ),
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Render the self-contained interactive HTML report."""
    from adlc.stages.report import run_report

    cfg = _cfg()
    rd = _rd(cfg, run_id)
    reduce_run(cfg, rd)
    result = run_report(cfg, rd, cdn_diagrams=cdn_diagrams)
    reduce_run(cfg, rd)
    if open_browser:
        import webbrowser

        webbrowser.open(rd.report.as_uri())
    _emit(result, as_json, f"report: {result['path']}")


@app.command()
def validate(
    run_id: str = typer.Argument("latest"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Validate run.json and taskgraph.json against their schemas."""
    from adlc.runs import read_json

    cfg = _cfg()
    rd = _rd(cfg, run_id)
    reduce_run(cfg, rd)

    findings: dict[str, Any] = {}
    ok = True
    run_valid, run_errors = is_valid("adlc-run", load_run(rd))
    findings["adlc-run"] = {"valid": run_valid, "errors": run_errors}
    ok &= run_valid

    if rd.taskgraph.is_file():
        graph_valid, graph_errors = is_valid("taskgraph", read_json(rd.taskgraph))
        findings["taskgraph"] = {"valid": graph_valid, "errors": graph_errors}
        ok &= graph_valid

    if rd.review_pack.is_file():
        pack_valid, pack_errors = is_valid("evidence-review-pack", read_json(rd.review_pack))
        findings["evidence-review-pack"] = {"valid": pack_valid, "errors": pack_errors}
        ok &= pack_valid

    if as_json:
        _emit({"valid": ok, "findings": findings}, True)
    else:
        for name, info in findings.items():
            mark = typer.style("valid  ", fg=typer.colors.GREEN) if info["valid"] \
                else typer.style("INVALID", fg=typer.colors.RED)
            typer.echo(f"  {mark} {name}")
            for err in info["errors"][:10]:
                typer.echo(f"          {err}")
    if not ok:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# ADRs / review / export
# ---------------------------------------------------------------------------


@adr_app.command("new")
def adr_new(
    title: str = typer.Argument(...),
    run_id: str = typer.Option("", "--run"),
    status: str = typer.Option("proposed", "--status"),
    context: str = typer.Option("", "--context"),
) -> None:
    """Create a MADR v4 architecture decision record."""
    from adlc.stages.adr import create_adr

    adr = create_adr(_cfg(), title, status=status, context=context, run_id=run_id)
    typer.secho(f"created {adr.path}", fg=typer.colors.GREEN)


@adr_app.command("list")
def adr_list(as_json: bool = typer.Option(False, "--json")) -> None:
    """List architecture decision records."""
    from adlc.stages.adr import list_adrs

    rows = [{"number": a.number, "title": a.title, "status": a.status, "path": str(a.path)}
            for a in list_adrs(_cfg())]
    _emit(rows, as_json,
          "\n".join(f"{r['number']}  {r['status']:<11} {r['title']}" for r in rows) or "no ADRs")


@adr_app.command("set-status")
def adr_set_status(
    number: str = typer.Argument(...),
    status: str = typer.Argument(...),
    review_sha: str = typer.Option("", "--review-sha"),
) -> None:
    """Set an ADR's status, optionally binding it to a review commit."""
    from adlc.stages.adr import set_status

    adr = set_status(_cfg(), number, status, review_sha=review_sha)
    typer.secho(f"{adr.number} -> {adr.status}", fg=typer.colors.GREEN)


@review_app.command("apply")
def review_apply(
    run_id: str = typer.Argument("latest"),
    event: Path = typer.Option(..., "--event", help="pull_request_review webhook payload."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Apply a native GitHub PR review as the human decision."""
    from adlc.stages.review import apply_review

    cfg = _cfg()
    rd = _rd(cfg, run_id)
    result = apply_review(cfg, rd, json.loads(event.read_text(encoding="utf-8")))
    reduce_run(cfg, rd)
    _emit(result, as_json, f"review: {(rd.latest_stage('review') or {}).get('message', '')}")
    if not result.get("applied"):
        raise typer.Exit(1)


@export_app.command("oes")
def export_oes(
    run_id: str = typer.Argument("latest"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Export an Open Experiment Specification document (comparative runs only)."""
    from adlc.config import load_adapters

    cfg = _cfg()
    rd = _rd(cfg, run_id)
    exporter_cls = load_adapters("export").get("oes")
    if exporter_cls is None:
        typer.secho(
            "OES exporter not installed (workstream L7 provides it)",
            fg=typer.colors.YELLOW, err=True,
        )
        raise typer.Exit(2)
    out = exporter_cls().export(load_run(rd), rd.path / "oes.json")
    _emit({"path": str(out)}, as_json, f"exported {out}")


@app.command()
def autoresearch(as_json: bool = typer.Option(False, "--json")) -> None:
    """Propose the next brief from repository and run history."""
    from adlc.stages.autoresearch import propose

    cfg = _cfg()
    result = propose(cfg)
    _emit(result, as_json, result.get("summary", "no proposal"))


@app.command()
def hotfix(
    incident: Path = typer.Option(..., "--incident", help="Incident payload (JSON or markdown)."),
    plan_only: bool = typer.Option(False, "--plan-only", help="Stop after planning the run."),
    runner: str | None = typer.Option(None, "--runner"),
    max_parallel: int | None = typer.Option(None, "--max-parallel"),
    allow_unqualified: bool = typer.Option(
        False, "--allow-unqualified", help="Proceed even if the incident scores below threshold."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Turn a production incident into a narrow, fully gated ADLC run.

    The day-2 entry point. It deliberately reuses the day-1 intake path, so a
    hotfix is specced, gated and recorded exactly like any other change rather
    than becoming an unaudited side channel.
    """
    from adlc.stages.hotfix import run_hotfix

    cfg = _cfg()
    result = run_hotfix(
        incident,
        cfg=cfg,
        plan_only=plan_only,
        runner_name=runner,
        max_parallel=max_parallel,
        allow_unqualified=allow_unqualified,
    )
    _emit(result, as_json, f"hotfix: {result.get('message', '')}")
    if result.get("status") == "fail":
        raise typer.Exit(1)


def main() -> None:  # pragma: no cover
    try:
        app()
    except KeyboardInterrupt:
        typer.secho("interrupted", fg=typer.colors.YELLOW, err=True)
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
