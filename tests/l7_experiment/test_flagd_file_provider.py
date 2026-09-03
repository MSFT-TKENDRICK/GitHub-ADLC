"""``FlagdFileProvider`` -- the spine's credential-free default flag backend.

Covers the parts that previously had no dedicated unit test: output path
resolution order, evaluation branches (not-found / disabled / targeting-match
/ default), and the ``feature_flag.set.id`` span attribute, which used to be
the literal string ``"adlc"`` for every run regardless of which run actually
produced the flag document (a bug flagged during L7/spine reconciliation:
``materialize`` writes ``metadata.flagSetId = f"adlc/{run_id}"`` but
``span_attributes`` read a hardcoded constant instead of that value). This
file pins the fix and would fail again if the two values ever drift back
apart.
"""

from __future__ import annotations

import json
from pathlib import Path

from adlc.adapters.flags.flagd_file import FlagdFileProvider
from adlc.config import Config


def _run(run_id: str = "2026-08-20-abcd", variants=None) -> dict:
    return {
        "runId": run_id,
        "variants": variants
        if variants is not None
        else [
            {"key": "control", "role": "control"},
            {"key": "candidate-a", "role": "candidate"},
        ],
    }


class TestDetect:
    def test_detect_is_always_true_credential_free(self, tmp_path: Path) -> None:
        cfg = Config(root=tmp_path)
        available, reason = FlagdFileProvider.detect(cfg)
        assert available is True
        assert "no daemon" in reason or "no" in reason.lower()


class TestMaterialize:
    def test_materialize_writes_one_variant_per_candidate(self, tmp_path: Path) -> None:
        provider = FlagdFileProvider(path=tmp_path / "flags.flagd.json")
        run = _run()
        target = provider.materialize(run)

        assert target == provider.path
        document = json.loads(target.read_text(encoding="utf-8"))
        flag_key = "adlc.exp.2026-08-20-abcd"
        assert document["flags"][flag_key]["variants"] == {
            "control": "control",
            "candidate-a": "candidate-a",
        }
        assert document["flags"][flag_key]["defaultVariant"] == "control"
        assert document["metadata"]["flagSetId"] == "adlc/2026-08-20-abcd"

    def test_materialize_defaults_to_a_single_control_variant_when_none_declared(
        self, tmp_path: Path
    ) -> None:
        provider = FlagdFileProvider(path=tmp_path / "flags.flagd.json")
        target = provider.materialize(_run(variants=[]))
        document = json.loads(target.read_text(encoding="utf-8"))
        flag_key = "adlc.exp.2026-08-20-abcd"
        assert document["flags"][flag_key]["variants"] == {"control": "control"}
        assert document["flags"][flag_key]["defaultVariant"] == "control"

    def test_materialize_falls_back_to_first_variant_when_no_control_role(
        self, tmp_path: Path
    ) -> None:
        provider = FlagdFileProvider(path=tmp_path / "flags.flagd.json")
        target = provider.materialize(
            _run(variants=[{"key": "only-one", "role": "candidate"}])
        )
        document = json.loads(target.read_text(encoding="utf-8"))
        flag_key = "adlc.exp.2026-08-20-abcd"
        assert document["flags"][flag_key]["defaultVariant"] == "only-one"

    def test_materialize_creates_parent_directories(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c" / "flags.flagd.json"
        provider = FlagdFileProvider(path=nested)
        provider.materialize(_run())
        assert nested.is_file()


class TestOutputPathResolution:
    """Explicit path > ADLC_RUN_DIR > Config.load().run_dir() > cwd fallback."""

    def test_explicit_constructor_path_wins(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("ADLC_RUN_DIR", str(tmp_path / "env-dir"))
        explicit = tmp_path / "explicit.json"
        provider = FlagdFileProvider(path=explicit)
        provider.materialize(_run())
        assert provider.path == explicit
        assert explicit.is_file()
        assert not (tmp_path / "env-dir").exists()

    def test_env_run_dir_used_when_no_explicit_path(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        env_dir = tmp_path / "env-dir"
        monkeypatch.setenv("ADLC_RUN_DIR", str(env_dir))
        provider = FlagdFileProvider()
        provider.materialize(_run())
        assert provider.path == env_dir / "flags.flagd.json"
        assert provider.path.is_file()

    def test_config_run_dir_used_when_no_explicit_path_or_env(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("ADLC_RUN_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        provider = FlagdFileProvider()
        provider.materialize(_run(run_id="run-xyz"))
        expected = Config.load().run_dir("run-xyz") / "flags.flagd.json"
        assert provider.path == expected
        assert provider.path.is_file()

    def test_falls_back_to_dot_adlc_runs_when_config_load_raises(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """If even ``Config.load()`` fails (e.g. a broken working directory),
        the provider must still produce a usable path rather than raising --
        materialisation is not allowed to fail just because run-dir resolution
        could not consult a config.
        """
        monkeypatch.delenv("ADLC_RUN_DIR", raising=False)
        monkeypatch.chdir(tmp_path)

        def _raise() -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(Config, "load", staticmethod(_raise))
        provider = FlagdFileProvider()
        provider.materialize(_run(run_id="run-fallback"))
        assert provider.path == Path(".adlc") / "runs" / "run-fallback" / "flags.flagd.json"
        assert provider.path.is_file()


class TestEvaluate:
    def test_evaluate_returns_flag_not_found_for_unknown_key(self, tmp_path: Path) -> None:
        provider = FlagdFileProvider(path=tmp_path / "flags.flagd.json")
        provider.materialize(_run())
        result = provider.evaluate("nonexistent.key", {})
        assert result == {"key": "nonexistent.key", "value": None, "variant": "", "reason": "FLAG_NOT_FOUND"}

    def test_evaluate_returns_disabled_when_state_is_not_enabled(self, tmp_path: Path) -> None:
        target = tmp_path / "flags.flagd.json"
        target.write_text(
            json.dumps(
                {
                    "flags": {
                        "adlc.exp.run-1": {
                            "state": "DISABLED",
                            "variants": {"control": "control"},
                            "defaultVariant": "control",
                        }
                    },
                    "metadata": {"flagSetId": "adlc/run-1"},
                }
            ),
            encoding="utf-8",
        )
        provider = FlagdFileProvider(path=target)
        result = provider.evaluate("adlc.exp.run-1", {})
        assert result["reason"] == "DISABLED"
        assert result["value"] is None

    def test_evaluate_honours_requested_variant_via_targeting_match(
        self, tmp_path: Path
    ) -> None:
        provider = FlagdFileProvider(path=tmp_path / "flags.flagd.json")
        provider.materialize(_run())
        result = provider.evaluate(
            "adlc.exp.2026-08-20-abcd", {"variant": "candidate-a"}
        )
        assert result == {
            "key": "adlc.exp.2026-08-20-abcd",
            "value": "candidate-a",
            "variant": "candidate-a",
            "reason": "TARGETING_MATCH",
        }

    def test_evaluate_ignores_requested_variant_not_in_definition(
        self, tmp_path: Path
    ) -> None:
        provider = FlagdFileProvider(path=tmp_path / "flags.flagd.json")
        provider.materialize(_run())
        result = provider.evaluate(
            "adlc.exp.2026-08-20-abcd", {"variant": "does-not-exist"}
        )
        assert result["reason"] == "DEFAULT"
        assert result["variant"] == "control"

    def test_evaluate_falls_back_to_default_variant_with_no_context(
        self, tmp_path: Path
    ) -> None:
        provider = FlagdFileProvider(path=tmp_path / "flags.flagd.json")
        provider.materialize(_run())
        result = provider.evaluate("adlc.exp.2026-08-20-abcd", {})
        assert result["reason"] == "DEFAULT"
        assert result["variant"] == "control"
        assert result["value"] == "control"

    def test_evaluate_reads_from_disk_when_not_yet_materialized_in_process(
        self, tmp_path: Path
    ) -> None:
        writer = FlagdFileProvider(path=tmp_path / "flags.flagd.json")
        writer.materialize(_run())

        reader = FlagdFileProvider(path=tmp_path / "flags.flagd.json")
        result = reader.evaluate("adlc.exp.2026-08-20-abcd", {})
        assert result["reason"] == "DEFAULT"

    def test_evaluate_with_no_file_and_no_prior_materialize_reports_not_found(
        self, tmp_path: Path
    ) -> None:
        provider = FlagdFileProvider(path=tmp_path / "does-not-exist.json")
        result = provider.evaluate("anything", {})
        assert result["reason"] == "FLAG_NOT_FOUND"


class TestSpanAttributes:
    """Pins the fix: feature_flag.set.id must equal the run's actual flagSetId,
    not a hardcoded constant shared by every run.
    """

    def test_span_attributes_reports_the_run_scoped_flag_set_id(
        self, tmp_path: Path
    ) -> None:
        provider = FlagdFileProvider(path=tmp_path / "flags.flagd.json")
        provider.materialize(_run(run_id="2026-08-20-abcd"))
        result = provider.evaluate("adlc.exp.2026-08-20-abcd", {"targetingKey": "user-1"})
        attributes = provider.span_attributes(result, {"targetingKey": "user-1"})

        assert attributes["feature_flag.set.id"] == "adlc/2026-08-20-abcd"

    def test_two_providers_for_different_runs_report_different_flag_set_ids(
        self, tmp_path: Path
    ) -> None:
        """The bug this test guards against: a hardcoded "adlc" literal makes
        every run's flag spans indistinguishable from one another downstream.
        """
        provider_a = FlagdFileProvider(path=tmp_path / "run-a" / "flags.flagd.json")
        provider_a.materialize(_run(run_id="run-a"))
        result_a = provider_a.evaluate("adlc.exp.run-a", {})
        attrs_a = provider_a.span_attributes(result_a, {})

        provider_b = FlagdFileProvider(path=tmp_path / "run-b" / "flags.flagd.json")
        provider_b.materialize(_run(run_id="run-b"))
        result_b = provider_b.evaluate("adlc.exp.run-b", {})
        attrs_b = provider_b.span_attributes(result_b, {})

        assert attrs_a["feature_flag.set.id"] != attrs_b["feature_flag.set.id"]
        assert attrs_a["feature_flag.set.id"] == "adlc/run-a"
        assert attrs_b["feature_flag.set.id"] == "adlc/run-b"

    def test_span_attributes_reads_flag_set_id_from_disk_when_read_only(
        self, tmp_path: Path
    ) -> None:
        """A provider that only ever reads (never materializes) must still
        report the correct flag-set id sourced from the file's own metadata.
        """
        writer = FlagdFileProvider(path=tmp_path / "flags.flagd.json")
        writer.materialize(_run(run_id="written-elsewhere"))

        reader = FlagdFileProvider(path=tmp_path / "flags.flagd.json")
        result = reader.evaluate("adlc.exp.written-elsewhere", {})
        attributes = reader.span_attributes(result, {})
        assert attributes["feature_flag.set.id"] == "adlc/written-elsewhere"

    def test_span_attributes_reason_is_lowercased(self, tmp_path: Path) -> None:
        provider = FlagdFileProvider(path=tmp_path / "flags.flagd.json")
        provider.materialize(_run())
        result = provider.evaluate("adlc.exp.2026-08-20-abcd", {})
        attributes = provider.span_attributes(result, {})
        assert attributes["feature_flag.result.reason"] == "default"

    def test_span_attributes_context_id_prefers_targeting_key(
        self, tmp_path: Path
    ) -> None:
        provider = FlagdFileProvider(path=tmp_path / "flags.flagd.json")
        provider.materialize(_run())
        result = provider.evaluate("adlc.exp.2026-08-20-abcd", {})
        attributes = provider.span_attributes(
            result, {"targetingKey": "tk", "id": "fallback-id"}
        )
        assert attributes["feature_flag.context.id"] == "tk"

    def test_span_attributes_context_id_falls_back_to_id(self, tmp_path: Path) -> None:
        provider = FlagdFileProvider(path=tmp_path / "flags.flagd.json")
        provider.materialize(_run())
        result = provider.evaluate("adlc.exp.2026-08-20-abcd", {})
        attributes = provider.span_attributes(result, {"id": "fallback-id"})
        assert attributes["feature_flag.context.id"] == "fallback-id"

    def test_span_attributes_all_semconv_keys_present(self, tmp_path: Path) -> None:
        provider = FlagdFileProvider(path=tmp_path / "flags.flagd.json")
        provider.materialize(_run())
        result = provider.evaluate("adlc.exp.2026-08-20-abcd", {})
        attributes = provider.span_attributes(result, {})
        for key in (
            "feature_flag.key",
            "feature_flag.provider.name",
            "feature_flag.result.variant",
            "feature_flag.result.value",
            "feature_flag.result.reason",
            "feature_flag.context.id",
            "feature_flag.set.id",
        ):
            assert key in attributes


class TestSignatureParity:
    """`FlagdFileProvider` and `LaunchDarklyProvider` must stay drop-in
    interchangeable: same constructor shape, same span_attributes signature.
    """

    def test_constructor_accepts_path_kwarg_like_launchdarkly(self, tmp_path: Path) -> None:
        from adlc.adapters.flags.launchdarkly import LaunchDarklyProvider

        flagd = FlagdFileProvider(path=tmp_path / "a.json")
        ld = LaunchDarklyProvider(path=tmp_path / "b.json")
        assert flagd.path == tmp_path / "a.json"
        assert ld.path == tmp_path / "b.json"

    def test_span_attributes_signature_matches_launchdarkly(self) -> None:
        import inspect

        from adlc.adapters.flags.launchdarkly import LaunchDarklyProvider

        flagd_sig = inspect.signature(FlagdFileProvider.span_attributes)
        ld_sig = inspect.signature(LaunchDarklyProvider.span_attributes)
        assert list(flagd_sig.parameters) == list(ld_sig.parameters)
