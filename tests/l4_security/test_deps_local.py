"""``adapters.gate.deps_local`` -- the credential-free default supply-chain gate.

Previously had no dedicated test file at 41% coverage. Covers ``detect()``
(no manifests, manifests-but-no-auditor, manifests-with-auditor), and
``evaluate()``'s every branch: no manifests (real pass), manifests present
but unaudited (`not_run`, fails a required gate), the pip-audit and npm-audit
engines (mocked subprocess output), severity-threshold pass/fail, and the
configurable ``depsMaxSeverity`` threshold. Subprocess calls are always
mocked -- this gate must never actually shell out during tests.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from adlc.adapters.gate.deps_local import DepsLocalGate
from adlc.config import Config


@pytest.fixture
def no_manifests_repo(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def pypi_repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def npm_repo(tmp_path: Path) -> Path:
    (tmp_path / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    return tmp_path


class TestDetect:
    def test_true_when_no_manifests_present(self, no_manifests_repo: Path) -> None:
        available, reason = DepsLocalGate.detect(Config(root=no_manifests_repo))
        assert available is True
        assert "no dependency manifests" in reason

    def test_false_when_manifests_present_but_no_auditor_on_path(
        self, pypi_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        available, reason = DepsLocalGate.detect(Config(root=pypi_repo))
        assert available is False
        assert "pyproject.toml" in reason
        assert "no auditor on PATH" in reason

    def test_true_when_pip_audit_available(
        self, pypi_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            shutil, "which", lambda name: "/usr/bin/pip-audit" if name == "pip-audit" else None
        )
        available, reason = DepsLocalGate.detect(Config(root=pypi_repo))
        assert available is True
        assert "pip-audit" in reason

    def test_true_when_npm_available(
        self, npm_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)
        available, reason = DepsLocalGate.detect(Config(root=npm_repo))
        assert available is True
        assert "npm" in reason


class TestEvaluateNoManifests:
    def test_no_manifests_is_a_real_pass(self, no_manifests_repo: Path) -> None:
        cfg = Config(root=no_manifests_repo)
        result = DepsLocalGate().evaluate({}, cfg)
        assert result["status"] == "pass"
        assert result["observed"]["findings"] == 0
        assert "nothing to audit" in result["message"]
        assert result["id"] == "deps_local"


class TestEvaluateUnauditedManifests:
    def test_pypi_manifest_with_no_pip_audit_is_not_run(
        self, pypi_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = Config(root=pypi_repo)
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        result = DepsLocalGate().evaluate({}, cfg)
        assert result["status"] == "not_run"
        assert "python (install pip-audit)" in result["message"]

    def test_npm_manifest_with_no_npm_is_not_run(
        self, npm_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = Config(root=npm_repo)
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        result = DepsLocalGate().evaluate({}, cfg)
        assert result["status"] == "not_run"
        assert "npm (install node/npm)" in result["message"]

    def test_not_run_is_required_by_default_and_therefore_fails_closed(
        self, pypi_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = Config(root=pypi_repo)
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        result = DepsLocalGate().evaluate({}, cfg)
        assert result["required"] is True
        assert result["status"] == "not_run"


class TestPipAuditEngine:
    def _mock_pip_audit(self, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/pip-audit" if name == "pip-audit" else None)
        fake_proc = MagicMock(stdout=json.dumps(payload), returncode=0)
        monkeypatch.setattr(
            "adlc.adapters.gate.deps_local.subprocess.run", lambda *a, **k: fake_proc
        )

    def test_no_vulnerabilities_passes(self, pypi_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mock_pip_audit(monkeypatch, {"dependencies": [{"name": "requests", "vulns": []}]})
        result = DepsLocalGate().evaluate({}, Config(root=pypi_repo))
        assert result["status"] == "pass"
        assert result["observed"]["total"] == 0

    def test_high_severity_vulnerability_fails_at_default_threshold(
        self, pypi_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_pip_audit(
            monkeypatch,
            {
                "dependencies": [
                    {"name": "requests", "vulns": [{"id": "GHSA-xxxx", "severity": "high"}]}
                ]
            },
        )
        result = DepsLocalGate().evaluate({}, Config(root=pypi_repo))
        assert result["status"] == "fail"
        assert result["observed"]["blocking"] == 1
        assert "engines" in result["observed"]
        assert "pip-audit" in result["observed"]["engines"]

    def test_low_severity_vulnerability_does_not_fail_default_threshold(
        self, pypi_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_pip_audit(
            monkeypatch,
            {"dependencies": [{"name": "requests", "vulns": [{"id": "GHSA-yyyy", "severity": "low"}]}]},
        )
        result = DepsLocalGate().evaluate({}, Config(root=pypi_repo))
        assert result["status"] == "pass"
        assert result["observed"]["total"] == 1
        assert result["observed"]["blocking"] == 0

    def test_configurable_threshold_lowers_the_bar(
        self, pypi_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_pip_audit(
            monkeypatch,
            {"dependencies": [{"name": "requests", "vulns": [{"id": "GHSA-zzzz", "severity": "low"}]}]},
        )
        cfg = Config(root=pypi_repo, raw={"gates": {"depsMaxSeverity": "low"}})
        result = DepsLocalGate().evaluate({}, cfg)
        assert result["status"] == "fail"

    def test_malformed_pip_audit_json_yields_no_findings_rather_than_crashing(
        self, pypi_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/pip-audit" if name == "pip-audit" else None)
        fake_proc = MagicMock(stdout="not json", returncode=1)
        monkeypatch.setattr(
            "adlc.adapters.gate.deps_local.subprocess.run", lambda *a, **k: fake_proc
        )
        result = DepsLocalGate().evaluate({}, Config(root=pypi_repo))
        assert result["status"] == "pass"
        assert result["observed"]["total"] == 0

    def test_pip_audit_bare_list_payload_shape_is_also_accepted(
        self, pypi_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_pip_audit(
            monkeypatch, [{"name": "flask", "vulns": [{"id": "GHSA-aaaa", "severity": "critical"}]}]
        )
        result = DepsLocalGate().evaluate({}, Config(root=pypi_repo))
        assert result["status"] == "fail"
        assert result["observed"]["blocking"] == 1


class TestNpmAuditEngine:
    def _mock_npm_audit(self, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)
        fake_proc = MagicMock(stdout=json.dumps(payload), returncode=0)
        monkeypatch.setattr(
            "adlc.adapters.gate.deps_local.subprocess.run", lambda *a, **k: fake_proc
        )

    def test_no_vulnerabilities_passes(self, npm_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mock_npm_audit(monkeypatch, {"vulnerabilities": {}})
        result = DepsLocalGate().evaluate({}, Config(root=npm_repo))
        assert result["status"] == "pass"

    def test_high_severity_npm_vulnerability_fails(
        self, npm_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_npm_audit(
            monkeypatch,
            {
                "vulnerabilities": {
                    "lodash": {
                        "severity": "high",
                        "via": [{"url": "https://github.com/advisories/GHSA-xxxx"}],
                    }
                }
            },
        )
        result = DepsLocalGate().evaluate({}, Config(root=npm_repo))
        assert result["status"] == "fail"
        assert "npm-audit" in result["observed"]["engines"]

    def test_npm_vulnerability_with_non_dict_via_entries_does_not_crash(
        self, npm_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_npm_audit(
            monkeypatch, {"vulnerabilities": {"lodash": {"severity": "low", "via": ["lodash"]}}}
        )
        result = DepsLocalGate().evaluate({}, Config(root=npm_repo))
        assert result["status"] == "pass"

    def test_malformed_npm_json_yields_no_findings(
        self, npm_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)
        fake_proc = MagicMock(stdout="not json", returncode=1)
        monkeypatch.setattr(
            "adlc.adapters.gate.deps_local.subprocess.run", lambda *a, **k: fake_proc
        )
        result = DepsLocalGate().evaluate({}, Config(root=npm_repo))
        assert result["status"] == "pass"
        assert result["observed"]["total"] == 0


class TestBothEcosystems:
    def test_pypi_and_npm_manifests_both_audited_in_one_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
        monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/tool")

        def _fake_run(argv: list[str], **kwargs: Any) -> MagicMock:
            if argv[0] == "pip-audit":
                return MagicMock(stdout=json.dumps({"dependencies": []}), returncode=0)
            return MagicMock(stdout=json.dumps({"vulnerabilities": {}}), returncode=0)

        monkeypatch.setattr("adlc.adapters.gate.deps_local.subprocess.run", _fake_run)
        result = DepsLocalGate().evaluate({}, Config(root=tmp_path))
        assert result["status"] == "pass"
        assert set(result["observed"]["engines"]) == {"pip-audit", "npm-audit"}
