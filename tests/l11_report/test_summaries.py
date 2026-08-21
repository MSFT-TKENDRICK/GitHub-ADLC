"""L11 — the 150-character summary contract.

Every navigable thing in the report carries a one-line summary, and the report's
cards are laid out against :data:`~adlc.summarize.TLDR_LIMIT`. Two properties are
load-bearing and are what these tests pin:

* the cap is never exceeded, for any input, including hostile ones; and
* the summary is *discriminating* -- two different things must not produce the
  same sentence, because an identical summary on every card looks like
  information while carrying none.
"""

from __future__ import annotations

import pytest

from adlc.summarize import (
    TLDR_LIMIT,
    adr_tldr,
    artifact_tldr,
    clamp,
    compose,
    gate_tldr,
    humanise_bytes,
    node_tldr,
    persona_tldr,
    requirement_tldr,
)

LONG = "Refactor " + " ".join(f"module{i}" for i in range(200))


class TestClamp:
    def test_short_text_is_returned_unchanged(self) -> None:
        assert clamp("A short summary.") == "A short summary."

    def test_whitespace_is_collapsed(self) -> None:
        assert clamp("a  b\n\tc   d") == "a b c d"

    def test_overlong_text_fits_the_budget(self) -> None:
        assert len(clamp(LONG)) <= TLDR_LIMIT

    def test_truncation_lands_on_a_word_boundary(self) -> None:
        out = clamp(LONG)
        assert out.endswith("...")
        assert not out[:-3].endswith(" "), "trailing space before the ellipsis reads as corruption"
        # The last kept token must be a whole word from the input.
        assert out[:-3].split()[-1] in LONG.split()

    def test_a_custom_limit_is_honoured(self) -> None:
        assert len(clamp(LONG, 40)) <= 40

    @pytest.mark.parametrize("value", ["", None, "   "])
    def test_empty_input_yields_empty_output(self, value: object) -> None:
        assert clamp(value) == ""  # type: ignore[arg-type]


class TestCompose:
    def test_the_first_clause_survives_and_later_ones_are_dropped(self) -> None:
        out = compose("Subject that matters", "x" * 200, limit=60)
        assert out.startswith("Subject that matters")
        assert len(out) <= 60

    def test_clauses_that_fit_are_all_kept(self) -> None:
        assert compose("One", "Two", "Three") == "One. Two. Three."

    def test_empty_clauses_are_skipped_not_rendered_as_gaps(self) -> None:
        assert compose("One", "", None, "Two") == "One. Two."  # type: ignore[arg-type]

    def test_no_clauses_yields_empty(self) -> None:
        assert compose("", None) == ""  # type: ignore[arg-type]

    def test_a_later_clause_is_kept_when_an_earlier_one_did_not_fit(self) -> None:
        """Fit is judged per clause, so a long clause does not veto a short one."""
        out = compose("Lead", "x" * 100, "Tail", limit=40)
        assert out == "Lead. Tail."


class TestNodeSummaries:
    def _node(self, **over: object) -> dict:
        node = {
            "id": "T001",
            "title": "Add dark mode to the settings page",
            "kind": "implement",
            "dependsOn": [],
            "writeSet": ["src/settings.py"],
            "acceptance": [],
        }
        node.update(over)
        return node

    def test_summary_fits_the_budget(self) -> None:
        assert len(node_tldr(self._node())) <= TLDR_LIMIT

    def test_kind_selects_a_plain_language_verb(self) -> None:
        assert node_tldr(self._node(kind="implement")).startswith("Builds")
        assert node_tldr(self._node(kind="test")).startswith("Proves")
        assert node_tldr(self._node(kind="doc")).startswith("Explains")
        assert node_tldr(self._node(kind="infra")).startswith("Wires up")

    def test_an_unknown_kind_falls_back_rather_than_crashing(self) -> None:
        assert node_tldr(self._node(kind="wat")).startswith("Builds")

    def test_the_leading_verb_is_not_doubled(self) -> None:
        assert "Builds add dark mode" not in node_tldr(self._node())
        assert "Builds dark mode" in node_tldr(self._node())

    def test_ordering_reflects_dependencies(self) -> None:
        assert "Starts in the first wave" in node_tldr(self._node(dependsOn=[]))
        assert "Waits for T001" in node_tldr(self._node(dependsOn=["T001"]))
        assert "Waits for 3 earlier tasks" in node_tldr(
            self._node(dependsOn=["T001", "T002", "T003"])
        )

    def test_the_write_set_says_where_the_change_lands(self) -> None:
        assert "Touches src/settings.py" in node_tldr(self._node())
        many = node_tldr(self._node(writeSet=["src/a.py", "src/b.py", "src/c.py"]))
        assert "Touches 3 files under src" in many

    def test_a_node_with_nothing_to_say_still_says_something(self) -> None:
        out = node_tldr({"id": "T9", "title": "", "kind": "implement"})
        assert out and len(out) <= TLDR_LIMIT

    def test_an_absurd_title_is_still_bounded(self) -> None:
        assert len(node_tldr(self._node(title=LONG))) <= TLDR_LIMIT

    def test_different_nodes_get_different_summaries(self) -> None:
        a = node_tldr(self._node(title="Add the toggle", writeSet=["src/toggle.py"]))
        b = node_tldr(self._node(title="Add the tests", kind="test", writeSet=["tests/t.py"]))
        assert a != b


class TestGateSummaries:
    @pytest.mark.parametrize(
        ("status", "required", "expected"),
        [
            ("pass", True, "Passed a check that must pass to merge"),
            ("pass", False, "Passed an advisory check"),
            ("fail", True, "Failed a check that must pass to merge"),
            ("fail", False, "Failed an advisory check"),
            ("not_run", True, "Never ran, and it had to"),
            ("not_run", False, "Never ran; it was optional"),
        ],
    )
    def test_the_lead_states_the_merge_consequence(
        self, status: str, required: bool, expected: str
    ) -> None:
        out = gate_tldr({"status": status, "required": required, "message": "m"})
        assert out.startswith(expected)

    def test_a_missing_required_gate_is_never_described_as_harmless(self) -> None:
        out = gate_tldr({"status": "not_run", "required": True, "message": ""})
        assert "blocks the merge" in out

    def test_summary_fits_the_budget(self) -> None:
        out = gate_tldr({"status": "fail", "required": True, "message": LONG})
        assert len(out) <= TLDR_LIMIT


class TestArtifactSummaries:
    @pytest.mark.parametrize(
        ("kind", "fragment"),
        [
            ("video", "screen recording"),
            ("screenshot", "still frame"),
            ("playwright_trace", "replayable trace"),
            ("har", "network request"),
            ("console_log", "logged"),
            ("persona_feedback", "walkthrough"),
        ],
    )
    def test_each_kind_is_explained_in_plain_language(self, kind: str, fragment: str) -> None:
        assert fragment in artifact_tldr({"kind": kind, "bytes": 10})

    def test_an_unknown_kind_degrades_to_a_readable_sentence(self) -> None:
        out = artifact_tldr({"kind": "flame_graph", "bytes": 1})
        assert "flame graph" in out

    def test_size_is_stated_in_human_terms(self) -> None:
        assert "1.0 KB" in artifact_tldr({"kind": "screenshot", "bytes": 1024})


class TestHumaniseBytes:
    @pytest.mark.parametrize(
        ("size", "expected"),
        [(0, "0 B"), (512, "512 B"), (1024, "1.0 KB"), (1024 * 1024, "1.0 MB")],
    )
    def test_units_scale(self, size: int, expected: str) -> None:
        assert humanise_bytes(size) == expected

    @pytest.mark.parametrize("value", [None, "not a number"])
    def test_unparseable_sizes_say_so_rather_than_claiming_zero(self, value: object) -> None:
        assert humanise_bytes(value) == "unknown size"


class TestAdrSummaries:
    @pytest.mark.parametrize(
        ("status", "fragment"),
        [
            ("accepted", "Decided and in force"),
            ("rejected", "Considered and turned down"),
            ("proposed", "Proposed, not yet decided"),
            ("deprecated", "No longer in force"),
            ("superseded", "Replaced by a later decision"),
        ],
    )
    def test_status_is_stated_as_a_consequence_not_a_label(
        self, status: str, fragment: str
    ) -> None:
        assert adr_tldr("Use tokens", status).startswith(fragment)

    def test_the_chosen_option_is_included_when_it_adds_information(self) -> None:
        assert "Chose CSS custom properties" in adr_tldr(
            "Adopt a theme", "accepted", "CSS custom properties"
        )

    def test_a_chosen_option_identical_to_the_title_is_not_repeated(self) -> None:
        out = adr_tldr("Use tokens", "accepted", "Use tokens")
        assert out.count("use tokens") == 1

    def test_summary_fits_the_budget(self) -> None:
        assert len(adr_tldr(LONG, "accepted", LONG)) <= TLDR_LIMIT


class TestRequirementSummaries:
    def test_uncovered_requirements_are_named_as_such(self) -> None:
        assert requirement_tldr("the toggle persists", False).startswith("No evidence attached")

    def test_covered_requirements_name_the_evidence_kinds(self) -> None:
        out = requirement_tldr("the toggle persists", True, ["screenshot", "video"])
        assert out.startswith("Backed by evidence")
        assert "screenshot, video" in out

    def test_summary_fits_the_budget(self) -> None:
        assert len(requirement_tldr(LONG, True, ["a", "b", "c", "d"])) <= TLDR_LIMIT


class TestPersonaSummaries:
    @pytest.mark.parametrize(
        ("verdict", "fragment"),
        [
            ("satisfied", "got through the task"),
            ("blocked", "could not finish the task"),
            ("confused", "had to guess"),
            ("partial", "only part of the task"),
        ],
    )
    def test_the_outcome_is_stated_from_the_persona_s_point_of_view(
        self, verdict: str, fragment: str
    ) -> None:
        assert fragment in persona_tldr("Ada", "reader", verdict)

    def test_friction_is_counted_and_pluralised(self) -> None:
        assert "Hit 1 friction point." in persona_tldr("Ada", "reader", "confused", 1)
        assert "Hit 3 friction points." in persona_tldr("Ada", "reader", "confused", 3)

    def test_no_friction_is_silence_not_a_zero(self) -> None:
        assert "friction" not in persona_tldr("Ada", "reader", "satisfied", 0)

    def test_the_scenario_distinguishes_one_persona_s_cards_from_each_other(self) -> None:
        """Without the scenario every card for a persona reads identically."""
        first = persona_tldr("Ada", "reader", "satisfied", scenario="enabling dark mode")
        second = persona_tldr("Ada", "reader", "satisfied", scenario="restoring the default")
        assert first != second
        assert "enabling dark mode" in first

    def test_summary_fits_the_budget(self) -> None:
        assert len(persona_tldr(LONG, LONG, "blocked", 4, scenario=LONG)) <= TLDR_LIMIT
