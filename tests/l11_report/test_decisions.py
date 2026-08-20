"""L11 — decision detail views and the "what informed this" citations pane.

A list of decision titles tells a reader that a choice was made. It does not let
them check whether the choice was reasonable. Two things close that gap, and
both are tested here.

**Citations are extracted, never invented.** Every link, file path, digest,
requirement id and run id a record mentions is pulled out and *classified*, so
the UI can turn a requirement id into a jump to that requirement instead of
rendering dead text. A record that cites nothing must be shown as citing
nothing -- an unsourced decision is a finding, and hiding it behind an empty
section would be the wrong kind of tidy.

**The decision knows which tasks it governs.** The linkage lives on the ADR
(``adlc-tasks``) rather than on the graph, because the graph is planned before
the decision is taken. This is what lets the report answer "where was this
decided?" in both directions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adlc.config import Config
from adlc.report.adr import build_adrs, parse_adr, parse_citations
from adlc.stages.adr import create_adr

LINKS = """
## Links

- [The spec](https://example.com/spec)
- `src/theme/tokens.css:L1-L40`
- Supersedes ADR-0002
- Requirement US1-AC1
- Recorded by `.adlc/runs/2026-08-20-c100`
- Artifact %s
""" % ("a" * 64)


def kinds(citations: list[dict[str, str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for citation in citations:
        out.setdefault(citation["kind"], []).append(citation["ref"])
    return out


class TestCitationExtraction:
    def test_every_kind_of_source_is_classified(self) -> None:
        found = kinds(parse_citations(LINKS, adr_numbers={"0002"}))
        assert found["web"] == ["https://example.com/spec"]
        assert found["file"] == ["src/theme/tokens.css"]
        assert found["adr"] == ["0002"]
        assert found["requirement"] == ["US1-AC1"]
        assert found["run"] == ["2026-08-20-c100"]
        assert found["artifact"] == ["a" * 64]

    def test_a_markdown_link_keeps_its_text_as_the_label(self) -> None:
        cited = parse_citations("[The spec](https://example.com/spec)")
        assert cited[0]["label"] == "The spec"

    def test_a_bare_url_is_picked_up(self) -> None:
        assert kinds(parse_citations("see https://example.com/x for detail"))["web"] == [
            "https://example.com/x"
        ]

    def test_trailing_punctuation_is_not_part_of_the_reference(self) -> None:
        assert parse_citations("see https://example.com/x.")[0]["ref"] == "https://example.com/x"

    def test_the_same_source_cited_twice_is_one_citation(self) -> None:
        text = "https://example.com/x and again https://example.com/x"
        assert len(parse_citations(text)) == 1

    def test_a_reference_to_a_decision_that_does_not_exist_is_dropped(self) -> None:
        """A dangling ADR link would render as a button that goes nowhere."""
        assert "adr" not in kinds(parse_citations("Supersedes ADR-0099", adr_numbers={"0001"}))

    def test_citations_are_grouped_by_kind_for_the_pane(self) -> None:
        order = [c["kind"] for c in parse_citations(LINKS, adr_numbers={"0002"})]
        assert order == sorted(order, key=lambda k: [
            "requirement", "artifact", "adr", "file", "web", "run", "anchor",
        ].index(k))

    def test_a_record_that_cites_nothing_yields_nothing(self) -> None:
        assert parse_citations("A decision made on vibes alone.") == []


class TestRecordParsing:
    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "0001-a-decision.md"
        path.write_text(body, encoding="utf-8")
        return path

    def test_sections_are_pulled_out_as_fields(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, (
            "---\nstatus: accepted\n---\n\n# Adopt tokens\n\n"
            "## Context and Problem Statement\n\nColours are hard-coded.\n\n"
            "## Decision Drivers\n\n* One source of truth\n\n"
            "## Considered Options\n\n* Tokens\n* Two stylesheets\n\n"
            "## Decision Outcome\n\nChosen option: \"Tokens\", because it is simplest.\n\n"
            "### Consequences\n\n* Colours must be tokenised.\n"
        ))
        parsed = parse_adr(path, path.read_text(encoding="utf-8"))
        assert parsed["title"] == "Adopt tokens"
        assert parsed["status"] == "accepted"
        assert parsed["context"] == "Colours are hard-coded."
        assert parsed["drivers"] == ["One source of truth"]
        assert parsed["options"] == ["Tokens", "Two stylesheets"]
        assert parsed["chosen"] == "Tokens"
        assert parsed["justification"] == "it is simplest."
        assert parsed["consequences"] == ["Colours must be tokenised."]

    def test_placeholder_bullets_are_not_rendered_as_content(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, (
            "---\nstatus: proposed\n---\n\n# T\n\n## Decision Drivers\n\n* _To be completed._\n"
        ))
        assert parse_adr(path, path.read_text(encoding="utf-8"))["drivers"] == []

    def test_an_unknown_section_is_kept_with_its_bullets_split_out(self, tmp_path: Path) -> None:
        """A maintainer who added a section meant it; it must not render as one blob."""
        path = self._write(tmp_path, (
            "---\nstatus: accepted\n---\n\n# T\n\n## Links\n\nSome prose.\n\n- one\n- two\n"
        ))
        section = parse_adr(path, path.read_text(encoding="utf-8"))["sections"][0]
        assert section["title"] == "Links"
        assert section["body"] == "Some prose."
        assert section["bullets"] == ["one", "two"]

    def test_an_empty_section_is_dropped(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "---\nstatus: accepted\n---\n\n# T\n\n## Empty\n\n## Links\n\n- a\n")
        titles = [s["title"] for s in parse_adr(path, path.read_text(encoding="utf-8"))["sections"]]
        assert titles == ["Links"]

    def test_a_record_with_no_front_matter_still_parses(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "# Just a title\n\n## Context and Problem Statement\n\nx\n")
        parsed = parse_adr(path, path.read_text(encoding="utf-8"))
        assert parsed["title"] == "Just a title"
        assert parsed["status"] == "unknown"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("T001, T002", ["T001", "T002"]),
            ("[T001, T002]", ["T001", "T002"]),
            ("T001", ["T001"]),
            ("n/a", []),
            ("", []),
            ("T001, T001", ["T001"]),
        ],
    )
    def test_task_references_tolerate_both_yaml_shapes(
        self, tmp_path: Path, value: str, expected: list[str]
    ) -> None:
        path = self._write(tmp_path, f"---\nstatus: accepted\nadlc-tasks: {value}\n---\n\n# T\n")
        assert parse_adr(path, path.read_text(encoding="utf-8"))["taskRefs"] == expected

    def test_the_summary_fits_a_card(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "---\nstatus: accepted\n---\n\n# " + "word " * 80 + "\n")
        assert len(parse_adr(path, path.read_text(encoding="utf-8"))["tldr"]) <= 150


class TestDecisionToTaskLinkage:
    def _graph(self) -> dict:
        return {"nodes": [
            {"id": "T001", "title": "Add the toggle"},
            {"id": "T002", "title": "Add the tests"},
        ]}

    def test_a_record_naming_its_tasks_is_linked_to_them(self, cfg: Config) -> None:
        create_adr(cfg, "Adopt tokens", status="accepted", tasks=["T001", "T002"])
        built = build_adrs(cfg, self._graph())
        assert [n["id"] for n in built[0]["nodes"]] == ["T001", "T002"]
        assert built[0]["nodes"][0]["title"] == "Add the toggle"

    def test_a_graph_node_naming_its_decision_is_linked_too(self, cfg: Config) -> None:
        """The older direction still works, so an existing graph is not orphaned."""
        create_adr(cfg, "Adopt tokens", status="accepted")
        graph = {"nodes": [{"id": "T001", "title": "Add the toggle", "adrRefs": ["ADR-0001"]}]}
        assert [n["id"] for n in build_adrs(cfg, graph)[0]["nodes"]] == ["T001"]

    def test_the_same_task_named_from_both_sides_is_listed_once(self, cfg: Config) -> None:
        create_adr(cfg, "Adopt tokens", status="accepted", tasks=["T001"])
        graph = {"nodes": [{"id": "T001", "title": "Add the toggle", "adrRefs": ["0001"]}]}
        assert len(build_adrs(cfg, graph)[0]["nodes"]) == 1

    def test_a_task_outside_this_run_s_graph_is_kept_but_flagged(self, cfg: Config) -> None:
        """A decision can outlive the plan that prompted it; dropping the ref would lie."""
        create_adr(cfg, "Adopt tokens", status="accepted", tasks=["T001", "T404"])
        nodes = {n["id"]: n for n in build_adrs(cfg, self._graph())[0]["nodes"]}
        assert nodes["T001"]["inGraph"] is True
        assert nodes["T404"]["inGraph"] is False

    def test_no_decisions_yields_an_empty_list_not_an_error(self, cfg: Config) -> None:
        assert build_adrs(cfg, self._graph()) == []

    def test_a_run_id_written_by_the_writer_is_read_back(self, cfg: Config) -> None:
        create_adr(cfg, "Adopt tokens", status="accepted", run_id="2026-08-20-c100")
        assert build_adrs(cfg)[0]["runId"] == "2026-08-20-c100"

    def test_citations_survive_the_round_trip_through_the_writer(self, cfg: Config) -> None:
        adr = create_adr(cfg, "Adopt tokens", status="accepted", run_id="2026-08-20-c100")
        with adr.path.open("a", encoding="utf-8") as handle:
            handle.write(LINKS)
        found = kinds(build_adrs(cfg)[0]["citations"])
        assert found["requirement"] == ["US1-AC1"]
        assert found["web"] == ["https://example.com/spec"]
        assert "2026-08-20-c100" in found["run"]
