"""``detect()`` contract for all three L3 eval backends.

The binding rule (CONTRIBUTING.md §4-5): with no credentials installed, every one of these
must report ``(False, "<specific reason>")`` so the spine's credential-free deterministic
runner stays in charge. ``detect()`` must also be cheap — no network, no subprocess.
"""

from __future__ import annotations

import shutil
from typing import Any

import pytest

from adlc.adapters.evals import assert_ as assert_mod
from adlc.adapters.evals.assert_ import AssertEvalRunner, EvalBackendUnavailable
from adlc.adapters.evals.azure import AzureEvalRunner
from adlc.adapters.evals.promptfoo import PromptfooEvalRunner
from adlc.config import Config, detect_all

RUNNERS = (AssertEvalRunner, PromptfooEvalRunner, AzureEvalRunner)
RUNNER_IDS = ("assert-ai", "promptfoo", "azure")


@pytest.mark.parametrize("runner", RUNNERS, ids=RUNNER_IDS)
@pytest.mark.usefixtures("no_tools", "no_subprocess")
def test_detect_reports_unavailable_without_tools_or_credentials(
    runner: type, cfg: Config
) -> None:
    available, reason = runner.detect(cfg)
    assert available is False
    assert isinstance(reason, str)
    # Specific, human-readable and actionable — it is surfaced verbatim in
    # capabilities.json and in any not_run gate.
    assert len(reason) > 30
    assert runner.name.split("-")[0] in reason.lower()
    assert any(hint in reason for hint in ("install", "configure", "set ")), reason


@pytest.mark.parametrize("runner", RUNNERS, ids=RUNNER_IDS)
@pytest.mark.usefixtures("no_tools", "no_subprocess")
def test_detect_never_raises_on_a_hostile_config(runner: type, cfg: Config) -> None:
    cfg.raw["eval"] = {"assert": None, "promptfoo": 17, "azure": ["nope"]}
    available, reason = runner.detect(cfg)
    assert available is False
    assert reason


@pytest.mark.parametrize("runner", RUNNERS, ids=RUNNER_IDS)
@pytest.mark.usefixtures("no_tools", "no_subprocess")
def test_run_refuses_instead_of_fabricating_a_pass(
    runner: type, cfg: Config, run_doc: dict[str, Any], rubric: dict[str, Any]
) -> None:
    with pytest.raises(EvalBackendUnavailable):
        runner(cfg).run(run_doc, rubric)


@pytest.mark.usefixtures("no_subprocess")
def test_assert_detect_flags_missing_credentials_when_cli_is_present(
    credential_free: None, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/assert-ai" if "assert" in name else None)
    available, reason = AssertEvalRunner.detect(cfg)
    assert available is False
    assert "credential" in reason


@pytest.mark.usefixtures("no_subprocess")
def test_assert_detect_flags_missing_target_when_credentialed(
    credential_free: None, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/assert-ai" if "assert" in name else None)
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    available, reason = AssertEvalRunner.detect(cfg)
    assert available is False
    assert "eval.assert.target" in reason

    cfg.raw["eval"] = {"assert": {"target": {"callable": "demo.app:chat"}}}
    available, reason = AssertEvalRunner.detect(cfg)
    assert available is True
    assert "assert-ai" in reason


@pytest.mark.usefixtures("no_subprocess")
def test_promptfoo_npx_fallback_is_opt_in(
    credential_free: None, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # npx alone must NOT make promptfoo look available: nearly every dev box has node,
    # and a false positive would displace the spine default and then fail at run time.
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/npx" if name == "npx" else None)
    available, reason = PromptfooEvalRunner.detect(cfg)
    assert available is False
    assert "opt-in" in reason

    monkeypatch.setenv("ADLC_PROMPTFOO_NPX", "1")
    available, reason = PromptfooEvalRunner.detect(cfg)
    assert available is False
    assert "credential" in reason  # now it is the missing judge key that blocks it


@pytest.mark.usefixtures("no_subprocess")
def test_azure_detect_requires_sdk_credentials_and_deployment(
    credential_free: None, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(assert_mod, "find_spec", lambda _name: object())
    available, reason = AzureEvalRunner.detect(cfg)
    assert available is False
    assert "credentials" in reason

    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "not-a-real-key")
    available, reason = AzureEvalRunner.detect(cfg)
    assert available is False
    assert "deployment" in reason

    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    available, reason = AzureEvalRunner.detect(cfg)
    assert available is True


@pytest.mark.usefixtures("no_tools", "no_subprocess")
def test_registered_eval_adapters_are_all_unavailable_credential_free(cfg: Config) -> None:
    """The conformance property: nothing L3 hijacks the eval seam without credentials."""
    detections = detect_all(cfg, "evals")
    for name in RUNNER_IDS:
        assert name in detections, f"{name} is not discoverable via its entry point"
        available, reason = detections[name]
        assert available is False, f"{name} claimed availability with no credentials"
        assert reason
