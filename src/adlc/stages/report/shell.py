"""The HTML shell and asset loading.

The shell is the only :func:`str.format` template left in the report, and it is
deliberately tiny: a head, a ``<div class="wrap">`` body slot, and the two
``<script>`` tags. The CSS and JS are **not** in this string -- they are loaded
from :mod:`adlc.stages.report.assets` and injected as *substituted values*.
Because ``str.format`` never rescans a substituted value, those assets may
contain as many ``{`` and ``}`` as CSS and JavaScript naturally do, with no
brace-doubling anywhere. That is the whole reason this layer exists.

Assets are read through :mod:`importlib.resources`, not ``Path(__file__)``, so
the report renders identically whether ``adlc`` is a source checkout or an
installed wheel (where the package may live inside a zip).
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files

from adlc.stages.report.context import ReportContext, escape

_ASSET_PACKAGE = "adlc.stages.report"

_SHELL = """<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ADLC run {run_id}</title>
<style>
{styles}
</style>
</head>
<body>
<div class="wrap">
{body}
</div>

<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<script>
{scripts}
</script>
</body>
</html>
"""


@cache
def read_asset(name: str) -> str:
    """Load a report asset by file name from the packaged ``assets/`` dir.

    Raises loudly if the asset is missing or empty. A silently empty asset would
    yield a report with no styling or no behaviour that still *looks* like it
    rendered -- exactly the kind of failure this indirection could otherwise
    hide.
    """
    text = files(_ASSET_PACKAGE).joinpath("assets", name).read_text(encoding="utf-8")
    if not text.strip():
        raise RuntimeError(f"report asset {name!r} is empty or missing")
    return text


def render_shell(ctx: ReportContext, body: str) -> str:
    """Wrap an assembled ``body`` in the shell, inlining the CSS and JS assets."""
    return _SHELL.format(
        run_id=escape(ctx.rd.run_id),
        styles=read_asset("base.css"),
        scripts=read_asset("app.js"),
        body=body,
    )
