"""``adapters.gate.secrets_local`` -- the credential-free default secrets gate.

Previously had no dedicated test file at 74% coverage. Covers `detect()`
(gitleaks present vs absent), `evaluate()` dispatching to the right engine,
the gitleaks JSON-report mapping (including malformed-output resilience),
and the built-in pattern scanner: every documented credential shape, the
directory/suffix/size skip rules, the 100-finding cap, and unreadable-file
resilience.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from adlc.adapters.gate.secrets_local import SecretsLocalGate
from adlc.config import Config


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(root=tmp_path)


class TestDetect:
    def test_true_when_gitleaks_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gitleaks" if name == "gitleaks" else None)
        available, reason = SecretsLocalGate.detect(Config(root=Path()))
        assert available is True
        assert "gitleaks" in reason

    def test_true_when_gitleaks_absent_with_builtin_fallback_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        available, reason = SecretsLocalGate.detect(Config(root=Path()))
        assert available is True
        assert "built-in pattern scan" in reason


class TestEvaluateDispatch:
    def test_dispatches_to_gitleaks_when_available(
        self, cfg: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gitleaks" if name == "gitleaks" else None)
        monkeypatch.setattr(
            "adlc.adapters.gate.secrets_local.subprocess.run",
            lambda *a, **k: MagicMock(stdout="[]", returncode=0),
        )
        result = SecretsLocalGate().evaluate({}, cfg)
        assert result["observed"]["engine"] == "gitleaks"
        assert result["status"] == "pass"

    def test_dispatches_to_builtin_when_gitleaks_absent(
        self, cfg: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        result = SecretsLocalGate().evaluate({}, cfg)
        assert result["observed"]["engine"] == "builtin"
        assert result["status"] == "pass"
        assert result["required"] is True
        assert result["severity"] == "critical"


class TestGitleaksEngine:
    def test_findings_are_mapped_and_fail_the_gate(
        self, cfg: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gitleaks" if name == "gitleaks" else None)
        payload = [{"RuleID": "generic-api-key", "File": "src/app.py", "StartLine": 12}]
        monkeypatch.setattr(
            "adlc.adapters.gate.secrets_local.subprocess.run",
            lambda *a, **k: MagicMock(stdout=json.dumps(payload), returncode=1),
        )
        result = SecretsLocalGate().evaluate({}, cfg)
        assert result["status"] == "fail"
        assert result["observed"]["findings"] == 1
        assert result["observed"]["detail"][0]["rule"] == "generic-api-key"

    def test_malformed_json_output_yields_no_findings(
        self, cfg: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gitleaks" if name == "gitleaks" else None)
        monkeypatch.setattr(
            "adlc.adapters.gate.secrets_local.subprocess.run",
            lambda *a, **k: MagicMock(stdout="not json", returncode=2),
        )
        result = SecretsLocalGate().evaluate({}, cfg)
        assert result["status"] == "pass"
        assert result["observed"]["findings"] == 0

    def test_empty_stdout_yields_no_findings(
        self, cfg: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gitleaks" if name == "gitleaks" else None)
        monkeypatch.setattr(
            "adlc.adapters.gate.secrets_local.subprocess.run",
            lambda *a, **k: MagicMock(stdout="", returncode=0),
        )
        result = SecretsLocalGate().evaluate({}, cfg)
        assert result["status"] == "pass"


class TestBuiltinEngine:
    def _run(self, cfg: Config, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        return SecretsLocalGate().evaluate({}, cfg)

    def test_no_findings_on_a_clean_repo(self, cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
        (cfg.root / "app.py").write_text("print('hello world')\n", encoding="utf-8")
        result = self._run(cfg, monkeypatch)
        assert result["status"] == "pass"
        assert result["observed"]["findings"] == 0

    @pytest.mark.parametrize(
        ("rule", "sample"),
        [
            ("github_token", "ghp_abcdefghijklmnopqrstuvwxyz012345"),
            ("github_fine_grained", "github_pat_" + "a" * 22),
            ("aws_access_key", "AKIAABCDEFGHIJKLMNOP"),
            ("azure_storage_key", "AccountKey=" + "A" * 88 + "=="),
            ("private_key", "-----BEGIN RSA PRIVATE KEY-----"),
            ("slack_token", "xoxb-1234567890-abcdefghij"),
            ("launchdarkly_sdk", "sdk-12345678-1234-1234-1234-123456789abc"),
            ("openai_key", "sk-" + "a" * 40),
        ],
    )
    def test_detects_every_documented_credential_shape(
        self, cfg: Config, monkeypatch: pytest.MonkeyPatch, rule: str, sample: str
    ) -> None:
        (cfg.root / "leak.txt").write_text(f"token = \"{sample}\"\n", encoding="utf-8")
        result = self._run(cfg, monkeypatch)
        assert result["status"] == "fail"
        assert any(f["rule"] == rule for f in result["observed"]["detail"])

    def test_skips_files_in_skip_directories(self, cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
        skip_dir = cfg.root / "node_modules"
        skip_dir.mkdir()
        (skip_dir / "leak.txt").write_text("ghp_abcdefghijklmnopqrstuvwxyz012345\n", encoding="utf-8")
        result = self._run(cfg, monkeypatch)
        assert result["status"] == "pass"

    def test_skips_binary_style_suffixes(self, cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
        (cfg.root / "image.png").write_bytes(b"ghp_abcdefghijklmnopqrstuvwxyz012345")
        result = self._run(cfg, monkeypatch)
        assert result["status"] == "pass"

    def test_skips_files_larger_than_the_size_cap(self, cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
        big = cfg.root / "big.txt"
        big.write_text("x" * 2_000_000 + "ghp_abcdefghijklmnopqrstuvwxyz012345\n", encoding="utf-8")
        monkeypatch.setattr(
            "adlc.adapters.gate.secrets_local.MAX_FILE_BYTES", 1000, raising=False
        )
        result = self._run(cfg, monkeypatch)
        assert result["status"] == "pass"

    def test_unreadable_file_does_not_crash_the_scan(
        self, cfg: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = cfg.root / "weird.txt"
        target.write_text("clean\n", encoding="utf-8")

        real_read_text = Path.read_text

        def _flaky_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            if self.name == "weird.txt":
                raise OSError("simulated unreadable file")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _flaky_read_text)
        result = self._run(cfg, monkeypatch)
        assert result["status"] == "pass"

    def test_finding_cap_stops_the_scan_at_100(self, cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
        token_lines = "\n".join(f"ghp_abcdefghijklmnopqrstuvwxyz{i:06d}" for i in range(150))
        (cfg.root / "many.txt").write_text(token_lines, encoding="utf-8")
        result = self._run(cfg, monkeypatch)
        assert result["status"] == "fail"
        assert result["observed"]["findings"] == 100
