"""L9 — the contract the spine's ``stages/enrich.py`` relies on.

Every generator in this leaf exposes exactly::

    def generate(run_dir: Path, spec_text: str, cfg: Config) -> list[Path]

The spine calls these opportunistically inside a ``try``/``except``, but the
except branch must never actually fire: a leaf that raises is a leaf that made
the run's enrichment stage non-deterministic. These tests hold that line for all
three modules at once.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from adlc.stages import enrich_diagrams, enrich_personas, enrich_wireframe

MODULES = (enrich_diagrams, enrich_personas, enrich_wireframe)
IDS = [m.__name__.rsplit(".", 1)[-1] for m in MODULES]


@pytest.mark.parametrize("module", MODULES, ids=IDS)
def test_signature_matches_the_spine_contract(module) -> None:
    assert callable(module.generate)
    params = list(inspect.signature(module.generate).parameters)
    assert params == ["run_dir", "spec_text", "cfg"]


@pytest.mark.parametrize("module", MODULES, ids=IDS)
def test_declares_a_facet_name(module) -> None:
    assert isinstance(module.FACET, str) and module.FACET


@pytest.mark.parametrize("module", MODULES, ids=IDS)
def test_returns_paths_under_the_enrichment_directory(
    module, run_dir: Path, spec_text: str, cfg
) -> None:
    for path in module.generate(run_dir, spec_text, cfg):
        assert isinstance(path, Path)
        assert path.is_file()
        assert path.parent == run_dir / "enrichment"


@pytest.mark.parametrize("module", MODULES, ids=IDS)
def test_every_facet_is_individually_skippable(module, run_dir: Path, spec_text: str) -> None:
    from adlc.config import Config

    only_this = Config(root=run_dir, raw={"enrich": {"skip": [module.FACET]}})
    assert module.generate(run_dir, spec_text, only_this) == []

    others = [m for m in MODULES if m is not module]
    for other in others:
        assert other.generate(run_dir, spec_text, only_this), (
            f"skipping {module.FACET} must not disable {other.FACET}"
        )


BAD_INPUTS = [
    pytest.param(None, None, id="all-none"),
    pytest.param(None, "# Spec\n\nAs a user, I want a thing.\n", id="none-rundir"),
    pytest.param(Path("run"), None, id="none-spec"),
    pytest.param(Path("run"), 1234, id="int-spec"),
    pytest.param(Path("run"), b"\xff\xfe binary", id="bytes-spec"),
    pytest.param(Path("run"), ["a", "list"], id="list-spec"),
    pytest.param(Path("run"), "\x00\x01\x02 \ufeff", id="control-chars"),
    pytest.param(Path("run"), "#" * 100_000, id="pathological-heading"),
    pytest.param(Path("run"), "As a " * 5_000, id="repeated-actor-fragment"),
    pytest.param(Path("run"), "|" * 50_000, id="pipes"),
]


@pytest.mark.parametrize("module", MODULES, ids=IDS)
@pytest.mark.parametrize(("run_arg", "spec_arg"), BAD_INPUTS)
def test_generate_never_raises(module, run_arg, spec_arg, tmp_path: Path, cfg) -> None:
    target = tmp_path / run_arg if isinstance(run_arg, Path) else run_arg
    result = module.generate(target, spec_arg, cfg)
    assert isinstance(result, list)
    assert all(isinstance(p, Path) for p in result)


@pytest.mark.parametrize("module", MODULES, ids=IDS)
def test_generate_never_raises_on_a_broken_config(module, tmp_path: Path, spec_text: str) -> None:
    class Exploding:
        @property
        def raw(self):  # noqa: ANN201 - deliberately hostile
            raise RuntimeError("config blew up")

    assert isinstance(module.generate(tmp_path / "run", spec_text, Exploding()), list)
    assert isinstance(module.generate(tmp_path / "run2", spec_text, object()), list)
    assert isinstance(module.generate(tmp_path / "run3", spec_text, None), list)


@pytest.mark.parametrize("module", MODULES, ids=IDS)
def test_generate_does_not_write_outside_enrichment(
    module, run_dir: Path, spec_text: str, cfg
) -> None:
    before = {p for p in run_dir.rglob("*") if p.is_file()}
    module.generate(run_dir, spec_text, cfg)
    after = {p for p in run_dir.rglob("*") if p.is_file()}
    for path in after - before:
        assert path.parent == run_dir / "enrichment", f"stray write: {path}"


def test_the_three_generators_compose_into_one_enrichment_directory(
    run_dir: Path, spec_text: str, cfg
) -> None:
    written: list[Path] = []
    for module in MODULES:
        written.extend(module.generate(run_dir, spec_text, cfg))
    assert {p.name for p in written} == {
        "architecture.mmd",
        "sitemap.mmd",
        "data-model.mmd",
        "personas.md",
        "wireframe.excalidraw",
    }
    assert len(written) == len(set(written)), "no two facets may claim the same path"
