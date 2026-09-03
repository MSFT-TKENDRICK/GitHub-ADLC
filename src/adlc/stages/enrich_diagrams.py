"""Mermaid diagram enrichment (leaf L9).

Produces ``architecture.mmd``, ``sitemap.mmd`` and ``data-model.mmd`` inside
``<run_dir>/enrichment/``.

Mermaid — not images — because GitHub renders ```mermaid fences natively in
Markdown and PR comments, the spine's ``report.html`` renders them with
mermaid.js, and the source stays diffable in git. See ``docs/enrichment.md``.

Everything here is a **pure, deterministic, offline heuristic** over the spec
text plus whatever Spec Kit wrote into ``<run_dir>/spec/``. No network, no LLM.
``generate()`` never raises: a failure returns ``[]`` and the run carries on.
"""

from __future__ import annotations

import itertools
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps import cost at zero
    from adlc.config import Config

log = logging.getLogger(__name__)

#: Marks this generator in ``.adlc/config.yaml`` -> ``enrich.skip``.
FACET = "diagrams"

# ---------------------------------------------------------------------------
# Mermaid validation
# ---------------------------------------------------------------------------

#: Diagram headers Mermaid accepts. A file that opens with anything else is a
#: silent no-render in both GitHub and mermaid.js, so we reject it up front.
MERMAID_HEADERS: frozenset[str] = frozenset(
    {
        "flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram",
        "stateDiagram-v2", "erDiagram", "journey", "gantt", "pie", "quadrantChart",
        "requirementDiagram", "gitGraph", "mindmap", "timeline", "zenuml",
        "sankey-beta", "xychart-beta", "block-beta", "packet-beta", "architecture-beta",
        "C4Context", "C4Container", "C4Component", "C4Dynamic", "C4Deployment",
    }
)

#: Directions accepted after ``flowchart`` / ``graph``.
FLOW_DIRECTIONS: frozenset[str] = frozenset({"TB", "TD", "BT", "RL", "LR"})

#: Node ids that collide with Mermaid grammar keywords.
RESERVED_IDS: frozenset[str] = frozenset({"end", "graph", "subgraph", "class", "click", "style"})

#: Left/right cardinality tokens of the ER relationship grammar. Entity names
#: may be bare words or quoted strings -- mermaid 11 accepts both.
_ER_REL = re.compile(
    r"^(?P<left>[A-Za-z_][\w-]*|\"[^\"]+\")\s+"
    r"(?P<lcard>\|\||\|o|\}\||\}o)"
    r"(?P<line>--|\.\.)"
    r"(?P<rcard>\|\||o\||\|\{|o\{)\s+"
    r"(?P<right>[A-Za-z_][\w-]*|\"[^\"]+\")\s*:\s*(?P<label>\S.*)$"
)

_ER_ATTRIBUTE = re.compile(r'^[A-Za-z_][\w\[\]]*\s+[A-Za-z_]\w*(\s+(PK|FK|UK))?(\s+".*")?\s*$')

_DANGLING_EDGE = re.compile(r"(-->|---|-\.->|-\.-|==>|===|~~~|--)\s*$")
_OPENERS = {"[": "]", "(": ")", "{": "}"}
_CLOSERS = {v: k for k, v in _OPENERS.items()}


def _strip_noise(source: str) -> list[tuple[int, str]]:
    """Return ``(line_no, text)`` for content lines, dropping comments/directives."""
    out: list[tuple[int, str]] = []
    for i, raw in enumerate(source.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("%%"):
            continue
        out.append((i, line))
    return out


def _scan_delimiters(source: str) -> list[str]:
    """Balance ``[] () {}`` and ``"`` across the document, ignoring quoted text."""
    errors: list[str] = []
    stack: list[tuple[str, int]] = []
    in_quotes = False
    quote_line = 0
    line_no = 1
    for ch in source:
        if ch == "\n":
            if in_quotes:
                errors.append(f"line {quote_line}: unterminated quoted label")
                in_quotes = False
            line_no += 1
            continue
        if ch == '"':
            in_quotes = not in_quotes
            quote_line = line_no
            continue
        if in_quotes:
            continue
        if ch in _OPENERS:
            stack.append((ch, line_no))
        elif ch in _CLOSERS:
            if not stack:
                errors.append(f"line {line_no}: unmatched '{ch}'")
            elif stack[-1][0] != _CLOSERS[ch]:
                opener, opened_at = stack.pop()
                errors.append(
                    f"line {line_no}: '{ch}' closes '{opener}' opened on line {opened_at}"
                )
            else:
                stack.pop()
    if in_quotes:
        errors.append(f"line {quote_line}: unterminated quoted label")
    for opener, opened_at in stack:
        errors.append(f"line {opened_at}: '{opener}' is never closed")
    return errors


def _scan_labels(line_no: int, line: str) -> list[str]:
    """Reject label text that would break the parser (bare pipes / quotes)."""
    errors: list[str] = []
    depth = 0
    buf: list[str] = []
    in_quotes = False
    for ch in line:
        if ch == '"':
            in_quotes = not in_quotes
            if depth:
                buf.append(ch)
            continue
        if in_quotes:
            if depth:
                buf.append(ch)
            continue
        if ch in _OPENERS:
            depth += 1
            if depth == 1:
                buf = []
                continue
        elif ch in _CLOSERS:
            depth -= 1
            if depth == 0:
                label = "".join(buf)
                if "|" in label:
                    errors.append(
                        f"line {line_no}: node label contains an unescaped '|': {label!r}"
                    )
                # Shape syntax nests delimiters -- (["x"]) and [("x")] -- so peel
                # them off before deciding whether the text itself is quoted.
                core = label.strip().strip("[]{}()")
                quoted = len(core) > 1 and core.startswith('"') and core.endswith('"')
                if '"' in core and not quoted:
                    errors.append(f"line {line_no}: node label has a stray '\"': {label!r}")
                elif quoted and '"' in core[1:-1]:
                    errors.append(f"line {line_no}: node label has a nested '\"': {label!r}")
                continue
            if depth < 0:
                depth = 0
                continue
        if depth:
            buf.append(ch)
    return errors


def _scan_pipes(line_no: int, line: str) -> list[str]:
    """Edge labels use ``-->|text|`` — pipes must pair up outside quotes."""
    count = 0
    in_quotes = False
    for ch in line:
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "|" and not in_quotes:
            count += 1
    if count % 2:
        return [f"line {line_no}: odd number of '|' — edge label is not closed"]
    return []


def validate_mermaid(source: str) -> tuple[bool, list[str]]:
    """Validate ``source`` as a Mermaid document.

    Returns ``(ok, errors)``. This is a structural linter, not a full parser: it
    catches the failure modes that actually happen when a diagram is generated
    from text — unknown header, unbalanced ``subgraph``/``end``, unbalanced
    brackets or quotes, pipes or quotes leaking into node labels, dangling
    edges, reserved node ids and malformed ER cardinalities. Those are exactly
    the cases that render as a blank box instead of an error.
    """
    errors: list[str] = []
    if not source or not source.strip():
        return False, ["empty diagram"]

    lines = _strip_noise(source)
    if not lines:
        return False, ["diagram contains only comments"]

    first_no, first = lines[0]
    header = first.split()[0] if first.split() else ""
    if header not in MERMAID_HEADERS:
        return False, [f"line {first_no}: unknown diagram type {header!r}"]
    if header in {"flowchart", "graph"}:
        parts = first.split()
        # Mermaid defaults the direction when it is omitted, so only a
        # present-but-unrecognised token is an error. Verified against the
        # mermaid 11 parser: `flowchart` parses, `flowchart XY` does not.
        if len(parts) > 1 and parts[1].rstrip(";") not in FLOW_DIRECTIONS:
            errors.append(
                f"line {first_no}: {parts[1]!r} is not a {header} direction "
                f"({', '.join(sorted(FLOW_DIRECTIONS))})"
            )

    # erDiagram cardinality tokens (`||--o{`, `}o--||`) contain unbalanced braces
    # by design, so the generic delimiter scan does not apply there; _validate_er
    # balances the attribute blocks instead.
    if header != "erDiagram":
        errors.extend(_scan_delimiters(source))

    subgraphs = 0
    for line_no, line in lines[1:]:
        lowered = line.lower()
        if lowered == "end" or lowered.startswith("end "):
            subgraphs -= 1
            if subgraphs < 0:
                errors.append(f"line {line_no}: 'end' without a matching 'subgraph'")
                subgraphs = 0
            continue
        if lowered.startswith("subgraph"):
            subgraphs += 1
        if header in {"flowchart", "graph"}:
            if _DANGLING_EDGE.search(line):
                errors.append(f"line {line_no}: edge has no target: {line!r}")
            errors.extend(_scan_pipes(line_no, line))
            errors.extend(_scan_labels(line_no, line))
            for token in re.findall(r"(?:^|\s)([A-Za-z_][\w-]*)\s*(?:\[|\(|\{|-->|---)", line):
                if token in RESERVED_IDS:
                    errors.append(f"line {line_no}: {token!r} is a reserved Mermaid keyword")
    if subgraphs > 0:
        errors.append(f"{subgraphs} 'subgraph' block(s) never closed with 'end'")

    if header == "erDiagram":
        errors.extend(_validate_er(lines[1:]))

    return (not errors), errors


def _validate_er(lines: list[tuple[int, str]]) -> list[str]:
    errors: list[str] = []
    in_block = False
    for line_no, line in lines:
        if in_block:
            if line == "}":
                in_block = False
            elif not _ER_ATTRIBUTE.match(line):
                errors.append(f"line {line_no}: bad ER attribute {line!r} (expected 'type name')")
            continue
        if line.endswith("{"):
            name = line[:-1].strip()
            if not re.match(r'^([A-Za-z_][\w-]*|"[^"]+")$', name):
                errors.append(f"line {line_no}: bad ER entity name {name!r}")
            in_block = True
            continue
        if "--" in line or ".." in line:
            if not _ER_REL.match(line):
                errors.append(f"line {line_no}: bad ER relationship {line!r}")
            continue
        if not re.match(r'^([A-Za-z_][\w-]*|"[^"]+")$', line):
            errors.append(f"line {line_no}: unrecognised erDiagram statement {line!r}")
    if in_block:
        errors.append("ER attribute block never closed with '}'")
    return errors


# ---------------------------------------------------------------------------
# Text sanitisation
# ---------------------------------------------------------------------------

_LABEL_BAD = re.compile(r"[^\w \-.,:/'&+%?!]")
_WS = re.compile(r"\s+")


def sanitize_label(text: str, limit: int = 60) -> str:
    """Make ``text`` safe to place inside a quoted Mermaid node label."""
    cleaned = _WS.sub(" ", _LABEL_BAD.sub(" ", text or "")).strip(" -.,:")
    cleaned = _WS.sub(" ", cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned or "unnamed"


def mermaid_id(prefix: str, text: str, index: int) -> str:
    """Build a collision-resistant, keyword-free node id."""
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:24]
    return f"{prefix}{index}_{slug}" if slug else f"{prefix}{index}"


def _normalize_for_match(text: str) -> str:
    """Upper-snake a phrase, splitting camel humps so ``ThemePreference`` and
    ``THEME_PREFERENCE`` compare equal."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text or "")
    return re.sub(r"[^A-Za-z0-9]+", "_", spaced).upper()


def er_name(text: str) -> str:
    """Normalise an entity name to the ER grammar (single bare word)."""
    slug = _normalize_for_match(text).strip("_")
    if not slug or slug[0].isdigit():
        slug = f"E_{slug}" if slug else "ENTITY"
    return slug[:40]


def entity_display(er_key: str) -> str:
    """Human-facing label for an ER entity key."""
    return " ".join(part.capitalize() for part in er_key.split("_") if part) or er_key


def er_token(text: str, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", (text or "").strip()).strip("_")
    if not slug or slug[0].isdigit():
        return fallback
    return slug[:40]


# ---------------------------------------------------------------------------
# Spec mining
# ---------------------------------------------------------------------------

_ACTOR = re.compile(r"\bAs an?\s+([a-z][a-z0-9 \-/]{2,40}?)\s*(?:,|\bI\b)", re.IGNORECASE)
_ROUTE_TOKEN = re.compile(r"^/[a-z0-9][a-z0-9\-_/{}:.]*$", re.IGNORECASE)
_ROUTE_INLINE = re.compile(r"[`\"']((?:/[A-Za-z0-9\-_{}:]+)+/?)[`\"']")
_ROUTE_HINT = re.compile(
    r"\b(route|routes|page|pages|screen|screens|view|views|url|path|nav|navigation"
    r"|endpoint|endpoints|sitemap)\b",
    re.IGNORECASE,
)
_SERVICE_HINT = re.compile(
    r"\b([A-Z][\w.-]*(?:[ \t]+[A-Z][\w.-]*){0,3}[ \t]+"
    r"(?:Service|API|Worker|Queue|Gateway|Handler|Store|Cache|Job|Daemon|Adapter|Provider))\b"
)
_ENTITY_BULLET = re.compile(r"^\s*[-*]\s+\*\*([A-Za-z][\w /-]{1,40})\*\*\s*[:\u2013\u2014-]?\s*(.*)$")
_ENTITY_HEADING = re.compile(
    r"^#{2,4}\s+(?:Entity[:\s]+)?([A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*){0,2})\s*$"
)
_ENTITY_SECTION = re.compile(r"\b(entit\w*|data\s+model)\b", re.IGNORECASE)
_FIELD_BULLET = re.compile(
    r"^\s*[-*]\s+`?\*{0,2}([A-Za-z_]\w*)\*{0,2}`?\s*[:(]\s*`?([A-Za-z_][\w\[\]<>. ]*)`?"
)
_TABLE_ROW = re.compile(r"^\s*\|\s*`?([A-Za-z_]\w*)`?\s*\|\s*`?([A-Za-z_][\w\[\]<>. ]*?)`?\s*\|")

#: Phrase -> (left cardinality, right cardinality) in Mermaid ER notation.
_RELATION_PHRASES: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    (
        re.compile(r"\bmany[- ]to[- ]many\b|\bN\s*:\s*M\b", re.IGNORECASE),
        "}o", "o{", "relates to",
    ),
    (
        re.compile(
            r"\bhas many\b|\bone[- ]to[- ]many\b|\b1\s*:\s*N\b|\bcontains many\b",
            re.IGNORECASE,
        ),
        "||", "o{", "has many",
    ),
    (
        re.compile(r"\bhas one\b|\bone[- ]to[- ]one\b|\b1\s*:\s*1\b", re.IGNORECASE),
        "||", "||", "has one",
    ),
    (
        re.compile(
            r"\bbelongs to\b|\bowned by\b|\breferences\b|\bpoints to\b",
            re.IGNORECASE,
        ),
        "}o", "||", "belongs to",
    ),
)

_STOP_ENTITIES = frozenset(
    {
        "given", "when", "then", "and", "note", "example", "summary", "overview", "scope",
        "goal", "goals", "requirements", "acceptance", "criteria", "success", "priority",
        "assumptions", "constraints", "risks", "edge", "cases", "out", "of", "must",
        "should", "system", "user story", "key entities", "functional requirements",
    }
)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


CONTRACT_SUFFIXES = frozenset({".yaml", ".yml", ".json", ".md", ".graphql"})


def read_spec_context(run_dir: Path, spec_text: str) -> dict[str, str]:
    """Collect the spec text Spec Kit produced, if any, alongside ``spec_text``."""
    spec_dir = run_dir / "spec"
    ctx = {
        "spec": spec_text or _read(spec_dir / "spec.md"),
        "plan": _read(spec_dir / "plan.md"),
        "data_model": _read(spec_dir / "data-model.md"),
        "contracts": "",
    }
    contracts = spec_dir / "contracts"
    if contracts.is_dir():
        chunks = []
        for path in sorted(contracts.iterdir()):
            if path.is_file() and path.suffix.lower() in CONTRACT_SUFFIXES:
                chunks.append(_read(path))
        ctx["contracts"] = "\n".join(chunks)
    return ctx


def feature_title(spec_text: str) -> str:
    for raw_line in (spec_text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("# "):
            title = line[2:].strip()
            title = re.sub(r"^(feature\s+specification|specification|spec|feature)\s*[:\-]\s*",
                           "", title, flags=re.IGNORECASE)
            return title.strip() or "Proposed change"
    return "Proposed change"


def extract_actors(text: str) -> list[str]:
    seen: list[str] = []
    for match in _ACTOR.finditer(text or ""):
        actor = match.group(1).strip().rstrip(",").strip()
        actor = re.sub(r"\b(who|that)\b.*$", "", actor, flags=re.IGNORECASE).strip()
        if 2 < len(actor) <= 40 and actor.lower() not in {a.lower() for a in seen}:
            seen.append(actor)
    return seen[:6]


def extract_routes(ctx: dict[str, str]) -> list[str]:
    """Routes from contracts (OpenAPI paths) and route-ish spec lines only.

    Ordered UI-first (shallowest non-``/api`` path wins) so that the first entry
    is a sensible entry point for the architecture diagram.
    """
    found: list[str] = []

    def add(candidate: str) -> None:
        route = candidate.strip().rstrip(",.;")
        # `{param}` would open an unbalanced brace in a flowchart label.
        route = re.sub(r"\{(\w+)\}", r":\1", route)
        if len(route) > 1 and route.endswith("/"):
            route = route[:-1]
        if not _ROUTE_TOKEN.match(route) or "." in route.rsplit("/", 1)[-1]:
            return
        if len(route) > 60 or route in found:
            return
        found.append(route)

    for line in (ctx.get("contracts") or "").splitlines():
        match = re.match(r"^\s{0,8}(/[A-Za-z0-9\-_{}/:]*)\s*:\s*$", line)
        if match:
            add(match.group(1))

    for blob in (ctx.get("spec"), ctx.get("plan")):
        for line in (blob or "").splitlines():
            for match in _ROUTE_INLINE.finditer(line):
                add(match.group(1))
            if _ROUTE_HINT.search(line):
                for token in re.findall(r"(?<![\w`\"'])(/[A-Za-z0-9\-_{}/:]+)", line):
                    add(token)

    found.sort(key=lambda r: (r.startswith("/api"), r.count("/"), r))
    return found[:12]


def extract_services(ctx: dict[str, str], fallback: str) -> list[str]:
    seen: list[str] = []
    for blob in (ctx.get("plan"), ctx.get("spec")):
        for match in _SERVICE_HINT.finditer(blob or ""):
            name = _WS.sub(" ", match.group(1)).strip()
            if len(name) <= 45 and name.lower() not in {s.lower() for s in seen}:
                seen.append(name)
    return seen[:6] or [fallback]


def extract_entities(ctx: dict[str, str]) -> dict[str, list[tuple[str, str]]]:
    """Return ``{ENTITY: [(type, field), ...]}`` mined from the spec/data model.

    Two structurally different sources, so two passes:

    * ``spec/data-model.md`` — Spec Kit writes one heading per entity with the
      fields as bullets or a markdown table underneath.
    * ``spec/spec.md`` — the ``Key Entities`` section lists entities as bold
      bullets with prose, and no field types.
    """
    entities: dict[str, list[tuple[str, str]]] = {}

    def register(name: str) -> str | None:
        clean = _WS.sub(" ", name).strip(" :*-")
        if not clean or clean.lower() in _STOP_ENTITIES or len(clean) > 40:
            return None
        if not re.match(r"^[A-Za-z][\w /-]*$", clean):
            return None
        key = er_name(clean)
        entities.setdefault(key, [])
        return key

    # Pass A: data-model.md -- headings are entities, bullets/rows are fields.
    current: str | None = None
    for line in (ctx.get("data_model") or "").splitlines():
        if line.lstrip().startswith("#"):
            heading = _ENTITY_HEADING.match(line.strip())
            current = register(heading.group(1)) if heading else None
            continue
        if current is None:
            continue
        field = _FIELD_BULLET.match(line) or _TABLE_ROW.match(line)
        if not field:
            continue
        name = er_token(field.group(1), "field")
        ftype = er_token(field.group(2), "string").lower()
        pair = (ftype, name)
        if pair not in entities[current] and len(entities[current]) < 12:
            entities[current].append(pair)

    # Pass B: spec.md -- bold bullets inside a "Key Entities" style section.
    in_section = False
    for line in (ctx.get("spec") or "").splitlines():
        if line.lstrip().startswith("#"):
            in_section = bool(_ENTITY_SECTION.search(line))
            continue
        if not in_section:
            continue
        bullet = _ENTITY_BULLET.match(line)
        if bullet:
            register(bullet.group(1))
    return entities


def extract_relationships(
    ctx: dict[str, str], entities: dict[str, list[tuple[str, str]]]
) -> list[tuple[str, str, str, str, str]]:
    """Relationships are only emitted when the spec actually states one."""
    if len(entities) < 2:
        return []
    names = list(entities)
    text = "\n".join(filter(None, (ctx.get("data_model"), ctx.get("spec"))))
    rels: list[tuple[str, str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for sentence in re.split(r"(?<=[.;\n])\s+", text):
        upper = _normalize_for_match(sentence)
        present = [
            n for n in names
            if re.search(rf"(?<![A-Z0-9]){re.escape(n)}(?![A-Z0-9])", upper)
        ]
        if len(present) < 2:
            continue
        for pattern, lcard, rcard, label in _RELATION_PHRASES:
            if not pattern.search(sentence):
                continue
            left, right = present[0], present[1]
            if left == right or (left, right) in seen or (right, left) in seen:
                break
            seen.add((left, right))
            rels.append((left, lcard, rcard, right, label))
            break
    return rels[:20]


# ---------------------------------------------------------------------------
# Diagram builders
# ---------------------------------------------------------------------------


def _header_comment(kind: str, title: str) -> str:
    subject = sanitize_label(title, 90)
    return f"%% {kind} — generated by adlc enrich (L9) from the spec for: {subject}"


def build_architecture(ctx: dict[str, str]) -> str:
    """A C4-ish container view: actors → UI surfaces → services → data.

    Edges are drawn **between layers**, not between individual nodes. The spec
    says which things exist; it almost never says which service calls which
    store, and inventing that wiring is exactly the kind of confident-but-wrong
    detail a downstream agent would then implement.
    """
    title = feature_title(ctx.get("spec", ""))
    actors = extract_actors(ctx.get("spec", "")) or ["User"]
    routes = extract_routes(ctx)
    services = extract_services(ctx, f"{title} service")
    entities = list(extract_entities(ctx))

    lines = [
        _header_comment("architecture", title),
        "%% Layer-to-layer edges only: the spec does not state node-level wiring.",
        "flowchart TB",
    ]
    layers: list[str] = []

    lines.append('    subgraph actors["Actors"]')
    for i, actor in enumerate(actors, 1):
        lines.append(f'        {mermaid_id("act", actor, i)}["{sanitize_label(actor.title())}"]')
    lines.append("    end")
    layers.append("actors")

    lines.append('    subgraph surfaces["UI surfaces"]')
    if routes:
        for i, route in enumerate(routes[:6], 1):
            lines.append(f'        {mermaid_id("ui", route, i)}["{sanitize_label(route)}"]')
    else:
        lines.append(f'        {mermaid_id("ui", title, 1)}["{sanitize_label(title)}"]')
    lines.append("    end")
    layers.append("surfaces")

    lines.append('    subgraph services["Services and logic"]')
    for i, service in enumerate(services, 1):
        lines.append(f'        {mermaid_id("svc", service, i)}(["{sanitize_label(service)}"])')
    lines.append("    end")
    layers.append("services")

    if entities:
        lines.append('    subgraph data["Data"]')
        for i, entity in enumerate(entities[:6], 1):
            label = sanitize_label(entity_display(entity))
            lines.append(f'        {mermaid_id("db", entity, i)}[("{label}")]')
        lines.append("    end")
        layers.append("data")

    for upstream, downstream in itertools.pairwise(layers):
        lines.append(f"    {upstream} --> {downstream}")
    return "\n".join(lines) + "\n"


def build_sitemap(ctx: dict[str, str]) -> str | None:
    routes = extract_routes(ctx)
    if not routes:
        return None
    title = feature_title(ctx.get("spec", ""))
    lines = [
        _header_comment("sitemap", title),
        "flowchart LR",
        '    root["/"]',
    ]
    ids: dict[str, str] = {"/": "root"}
    edges: list[str] = []
    for route in routes:
        segments = [s for s in route.split("/") if s]
        for depth in range(len(segments)):
            path = "/" + "/".join(segments[: depth + 1])
            if path in ids:
                continue
            node_id = mermaid_id("pg", path, len(ids))
            ids[path] = node_id
            lines.append(f'    {node_id}["{sanitize_label(path)}"]')
            parent = "/" + "/".join(segments[:depth]) if depth else "/"
            edges.append(f"    {ids.get(parent, 'root')} --> {node_id}")
    lines.extend(edges)
    return "\n".join(lines) + "\n"


def build_data_model(ctx: dict[str, str]) -> str | None:
    entities = extract_entities(ctx)
    if not entities:
        return None
    title = feature_title(ctx.get("spec", ""))
    lines = [
        _header_comment("data model", title),
        "erDiagram",
    ]
    for left, lcard, rcard, right, label in extract_relationships(ctx, entities):
        lines.append(f"    {left} {lcard}--{rcard} {right} : {er_token(label, 'relates')}")
    for name, fields in entities.items():
        if not fields:
            lines.append(f"    {name}")
            continue
        lines.append(f"    {name} {{")
        for ftype, fname in fields:
            lines.append(f"        {ftype} {fname}")
        lines.append("    }")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _skipped(cfg: Config | None) -> bool:
    try:
        skip = ((getattr(cfg, "raw", None) or {}).get("enrich") or {}).get("skip") or []
        return FACET in skip
    except Exception:  # noqa: BLE001 - config shape is not ours to trust
        return False


def generate(run_dir: Path, spec_text: str, cfg: Config) -> list[Path]:
    """Write Mermaid diagrams into ``run_dir/enrichment`` and return their paths.

    Never raises. An unusable spec, an unwritable directory or a diagram that
    fails validation simply yields fewer paths — possibly ``[]``.
    """
    written: list[Path] = []
    try:
        if _skipped(cfg):
            log.info("enrich_diagrams: skipped via config (enrich.skip contains %r)", FACET)
            return []
        run_dir = Path(run_dir)
        ctx = read_spec_context(run_dir, spec_text)
        if not (ctx.get("spec") or "").strip():
            log.warning("enrich_diagrams: no spec text available, nothing to diagram")
            return []

        out_dir = run_dir / "enrichment"
        out_dir.mkdir(parents=True, exist_ok=True)

        builders = (
            ("architecture.mmd", build_architecture),
            ("sitemap.mmd", build_sitemap),
            ("data-model.mmd", build_data_model),
        )
        for filename, builder in builders:
            try:
                diagram = builder(ctx)
            except Exception:
                log.exception("enrich_diagrams: %s builder failed", filename)
                continue
            if not diagram:
                log.info("enrich_diagrams: %s skipped, spec has nothing to model", filename)
                continue
            ok, errors = validate_mermaid(diagram)
            if not ok:
                log.error("enrich_diagrams: refusing to write invalid %s: %s", filename, errors)
                continue
            path = out_dir / filename
            path.write_text(diagram, encoding="utf-8")
            written.append(path)
    except Exception:
        log.exception("enrich_diagrams: generation failed")
        return written
    return written
