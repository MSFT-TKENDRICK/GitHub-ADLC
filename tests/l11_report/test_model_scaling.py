"""L11 — the model is built once, so it may not redo work per node.

``build_model`` runs on every render, and :mod:`adlc.report.model` opens by
claiming that is "what keeps a 60-file, 200-artifact run instant on a laptop with
no network". ``_node_artifacts`` quietly broke that claim: it case-folded the
*entire* artifact list once for every node, so a 60-node run re-lowercased 200
paths 60 times and allocated 12,000 throwaway strings to answer 60 questions.

The shape is the one ``media._pair_shots`` was already rewritten to remove, one
file over: work that belongs to the collection being done once per item that
scans it. The fix hoists the fold into :func:`_artifact_paths`.

These tests count the folding rather than timing it, because a timing assertion
on a small fixture is noise and a timing assertion on a large one is slow.
"""

from __future__ import annotations

import json

from adlc.report.model import _artifact_paths, _node_artifacts, build_model
from adlc.runs import RunDir


class CountingPath:
    """A path that records every time something asks for its string form.

    ``_artifact_paths`` reaches the value through ``str(...)``, so counting
    ``__str__`` counts exactly the work under test.
    """

    def __init__(self, value: str, counter: list[int]) -> None:
        self._value = value
        self._counter = counter

    def __str__(self) -> str:
        self._counter[0] += 1
        return self._value


def _artifacts(count: int, counter: list[int]) -> list[dict[str, object]]:
    return [
        {"sha256": f"sha-{i:03d}", "path": CountingPath(f"runs/x/T{i:03d}/out.txt", counter)}
        for i in range(count)
    ]


class TestFoldingHappensOncePerArtifact:
    def test_each_path_is_read_exactly_once_to_build_the_index(self) -> None:
        counter = [0]
        artifacts = _artifacts(50, counter)
        _artifact_paths(artifacts)
        assert counter[0] == 50

    def test_querying_many_nodes_does_not_touch_the_paths_again(self) -> None:
        """The regression: this was 50 x 40 = 2,000 folds, not 50."""
        counter = [0]
        paths = _artifact_paths(_artifacts(50, counter))
        assert counter[0] == 50

        for i in range(40):
            _node_artifacts({"id": f"T{i:03d}"}, paths)

        assert counter[0] == 50, (
            f"artifact paths were re-read {counter[0] - 50} extra times while "
            "answering 40 nodes; the case-fold has fallen back inside the per-node "
            "scan and the render is quadratic again"
        )

    def test_the_cost_does_not_grow_with_the_node_count(self) -> None:
        """Same artifacts, ten times the nodes, identical string work."""
        readings = []
        for node_count in (10, 100):
            counter = [0]
            paths = _artifact_paths(_artifacts(30, counter))
            for i in range(node_count):
                _node_artifacts({"id": f"T{i:03d}"}, paths)
            readings.append(counter[0])
        assert readings[0] == readings[1] == 30


class TestMatchingIsUnchanged:
    """The speed-up must not move the answer."""

    def test_a_node_finds_the_artifacts_naming_it(self) -> None:
        counter = [0]
        paths = _artifact_paths(_artifacts(5, counter))
        assert _node_artifacts({"id": "T003"}, paths) == ["sha-003"]

    def test_matching_stays_case_insensitive(self) -> None:
        counter = [0]
        paths = _artifact_paths(
            [{"sha256": "s1", "path": CountingPath("runs/X/T007/OUT.TXT", counter)}]
        )
        assert _node_artifacts({"id": "t007"}, paths) == ["s1"]

    def test_a_node_matching_nothing_gets_nothing(self) -> None:
        counter = [0]
        paths = _artifact_paths(_artifacts(5, counter))
        assert _node_artifacts({"id": "T999"}, paths) == []

    def test_an_id_less_node_never_matches_everything(self) -> None:
        """An empty id is a substring of every path; it must return nothing."""
        counter = [0]
        paths = _artifact_paths(_artifacts(5, counter))
        for node in ({"id": ""}, {}, {"id": None}):
            assert _node_artifacts(node, paths) == []

    def test_a_missing_path_is_tolerated(self) -> None:
        assert _artifact_paths([{"sha256": "s1"}]) == [("s1", "")]


class TestTheFoldIsHoistedOutOfTheNodeLoop:
    """The unit tests above pin the contract; this pins the call site.

    ``_node_artifacts`` can only stay linear if ``build_model`` folds *once*,
    before it walks the nodes. Moving that call back inside the loop would
    restore the original quadratic render while every unit test above still
    passed, so the number of folds per render is asserted directly.
    """

    def test_build_model_folds_once_regardless_of_node_count(
        self, cfg, monkeypatch
    ) -> None:
        from adlc.report import model as model_module

        rd = RunDir(cfg, "r-scaling")
        rd.path.mkdir(parents=True, exist_ok=True)
        artifacts = [
            {"sha256": f"sha-{i:03d}", "path": f"runs/x/T{i:03d}/out.txt"}
            for i in range(20)
        ]
        rd.run_json.write_text(
            json.dumps(
                {"id": "r-scaling", "artifacts": artifacts, "gates": [], "stages": []}
            ),
            encoding="utf-8",
        )
        rd.taskgraph.write_text(
            json.dumps(
                {
                    "nodes": [
                        {"id": f"T{i:03d}", "title": f"task {i}", "level": 0,
                         "dependsOn": []}
                        for i in range(25)
                    ]
                }
            ),
            encoding="utf-8",
        )

        folds: list[int] = []
        real = model_module._artifact_paths

        def counting(arts: list[dict[str, object]]) -> list[tuple[str, str]]:
            folds.append(len(arts))
            return real(arts)

        monkeypatch.setattr(model_module, "_artifact_paths", counting)
        model = build_model(cfg, rd)

        assert len(model["graph"]["nodes"]) == 25, "the fixture must exercise the loop"
        assert folds == [20], (
            f"build_model folded the artifact list {len(folds)} times for 25 nodes; "
            "the call belongs above the node loop, not inside it"
        )
