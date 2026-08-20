"""Summary section: run header, pass/fail banner, and the stat grid."""

from __future__ import annotations

from adlc.runs import utcnow
from adlc.stages.report.context import ReportContext, escape


def render(ctx: ReportContext) -> str:
    run = ctx.run
    run_id = escape(ctx.rd.run_id)
    repo = escape(ctx.repo or "unknown repository")
    profile = escape(str(run.get("profile", "")))
    status = escape(str(run.get("status", "")))
    base_sha = escape((run.get("baseSha") or "")[:12])
    head_sha = escape((run.get("headSha") or "")[:12])
    generated = escape(utcnow())

    banner_cls = "ok" if ctx.passed else "bad"
    banner_text = "All required gates passed" if ctx.passed else "Required gates did not pass"
    required_total = sum(1 for g in ctx.gates if g.get("required"))
    required_pass = sum(1 for g in ctx.gates if g.get("required") and g.get("status") == "pass")
    failure_list = (
        "<ul>" + "".join(f"<li>{escape(f)}</li>" for f in ctx.failures) + "</ul>"
        if ctx.failures
        else ""
    )

    qual = ctx.qualification
    qualification = (
        f'{qual["score"]}/100 &middot; {escape(qual["category"])} &middot; risk {escape(qual["risk"])}'
        if qual
        else "not qualified"
    )

    stage_count = len(ctx.stages)
    variant_count = len(run.get("variants") or [])
    artifact_count = len(ctx.artifacts)

    return "\n".join(
        [
            '  <header class="top">',
            "    <div>",
            f'      <h1>ADLC run <span class="mono">{run_id}</span></h1>',
            f'      <div class="sub">{repo} &middot; profile <strong>{profile}</strong> &middot;',
            f"        status <strong>{status}</strong> &middot;",
            f'        <span class="mono">{base_sha}</span> &rarr; <span class="mono">{head_sha}</span></div>',
            f'      <div class="sub">Generated {generated}</div>',
            "    </div>",
            '    <button id="theme">Toggle theme</button>',
            "  </header>",
            "",
            f'  <div class="banner {banner_cls}">',
            f"    <strong>{banner_text}</strong>",
            f'    <div class="sub">{required_pass} of {required_total} required gates passing.',
            "      A required gate that did not run counts as a failure &mdash; absence of evidence is not evidence of correctness.</div>",
            f"    {failure_list}",
            "  </div>",
            "",
            '  <div class="grid">',
            f'    <div class="stat"><div class="k">Qualification</div><div class="v" style="font-size:15px">{qualification}</div></div>',
            f'    <div class="stat"><div class="k">Stages</div><div class="v">{stage_count}</div></div>',
            f'    <div class="stat"><div class="k">Variants</div><div class="v">{variant_count}</div></div>',
            f'    <div class="stat"><div class="k">Artifacts</div><div class="v">{artifact_count}</div></div>',
            "  </div>",
        ]
    )
