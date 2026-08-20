"""LaunchDarkly :class:`~adlc.ports.FlagProvider`, wired through OpenFeature.

**Scope, stated up front:** LaunchDarkly is used here for flag *delivery* and
metric *emission* only. It is explicitly **not a gate authority** in this design
— its experiment-results read API is unverified, so nothing in ADLC ever gates a
merge on a LaunchDarkly verdict. Gate decisions come from ``adlc.adapters.gate.*``
and are recorded in ``adlc-run/v1``; see ``docs/experiments.md``.

The adapter talks to LaunchDarkly through the **OpenFeature** provider
(``launchdarkly-openfeature-server``) rather than the raw LaunchDarkly SDK, so
the application-facing API stays vendor-neutral: swapping this adapter for the
spine's flagd file provider changes no call site.

Availability is opt-in and degrades cleanly. With no ``LAUNCHDARKLY_SDK_KEY``,
:meth:`LaunchDarklyProvider.detect` returns ``(False, reason)`` and
``adlc.config.select_adapter`` falls back to the spine's credential-free
``flagd-file`` default, so the conformance suite is unaffected.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from adlc.stages.experiment import flag_evaluation_attributes

if TYPE_CHECKING:  # pragma: no cover
    from adlc.config import Config
    from adlc.ports import FlagResult, Run

_LOG = logging.getLogger(__name__)

#: The only credential this adapter reads. Never logged, never written to a run.
SDK_KEY_ENV = "LAUNCHDARKLY_SDK_KEY"

#: Optional, for labelling the manifest only.
PROJECT_ENV = "LAUNCHDARKLY_PROJECT"
ENVIRONMENT_ENV = "LAUNCHDARKLY_ENVIRONMENT"

#: PyPI distributions this adapter needs, and the modules they provide.
REQUIRED_MODULES: tuple[tuple[str, str], ...] = (
    ("openfeature", "openfeature-sdk"),
    ("ldclient", "launchdarkly-server-sdk"),
    ("ld_openfeature", "launchdarkly-openfeature-server"),
)

#: OpenFeature domain used for the client this adapter binds.
OPENFEATURE_DOMAIN = "adlc"

#: Filename written by :meth:`LaunchDarklyProvider.materialize`.
MANIFEST_NAME = "flags.launchdarkly.json"

MANIFEST_SCHEMA_VERSION = "adlc-flag-manifest/v1"


class LaunchDarklyProvider:
    """Deliver ADLC experiment flags via LaunchDarkly through OpenFeature.

    Registered as the ``launchdarkly`` entry point in ``adlc.flags``.
    """

    name = "launchdarkly"
    kind = "flags"

    def __init__(
        self,
        run_dir: Path | str | None = None,
        *,
        telemetry: Any = None,
        client: Any = None,
    ) -> None:
        self._run_dir = Path(run_dir) if run_dir else None
        self._telemetry = telemetry
        self._client = client

    # -- availability -----------------------------------------------------

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        """Cheap, offline, non-raising probe.

        Checks only for the credential in the environment and for importable
        modules — no network call, no SDK initialization (which would open a
        streaming connection), no subprocess.
        """
        try:
            if not (os.environ.get(SDK_KEY_ENV) or "").strip():
                return False, (
                    f"{SDK_KEY_ENV} is not set; LaunchDarkly flag delivery is unavailable and "
                    "the spine's credential-free flagd-file provider will be used instead"
                )
            missing = [
                distribution
                for module, distribution in REQUIRED_MODULES
                if importlib.util.find_spec(module) is None
            ]
        except Exception as exc:  # noqa: BLE001 - detect() must never raise
            return False, f"LaunchDarkly probe failed: {exc}"
        if missing:
            return False, (
                f"{SDK_KEY_ENV} is set but {', '.join(missing)} is not installed "
                "(pip install 'adlc[flags]' launchdarkly-openfeature-server)"
            )
        return True, (
            f"{SDK_KEY_ENV} is set and the OpenFeature LaunchDarkly provider is importable; "
            "flag delivery and metric emission only — LaunchDarkly never gates a run"
        )

    # -- FlagProvider -----------------------------------------------------

    def materialize(self, run: Run | Mapping[str, Any]) -> Path:
        """Write the flag manifest this run expects LaunchDarkly to serve.

        LaunchDarkly flags live server-side and an SDK key is read-only, so
        unlike the flagd file provider this cannot *create* the flags. It writes
        a declarative manifest instead: the variant → flag-key mapping, the
        target project/environment, and the expected variations. That artifact is
        what an operator (or a Terraform/LD API step) provisions from, and it is
        hashable evidence of what the run intended to serve.
        """
        run_id = str(run.get("runId") or "unknown")
        variants = [v for v in (run.get("variants") or []) if isinstance(v, Mapping)]

        flags: dict[str, Any] = {}
        for variant in variants:
            key = str(variant.get("key") or variant.get("id") or "")
            role = variant.get("role")
            for flag_key in variant.get("flagKeys") or variant.get("featureFlagKeys") or []:
                entry = flags.setdefault(
                    str(flag_key),
                    {"key": str(flag_key), "kind": "string", "variations": [], "servedTo": {}},
                )
                if key and key not in entry["variations"]:
                    entry["variations"].append(key)
                if key:
                    entry["servedTo"][key] = {"role": role, "commit": variant.get("commit")}

        manifest = {
            "schemaVersion": MANIFEST_SCHEMA_VERSION,
            "provider": self.name,
            "runId": run_id,
            "flagSetId": run_id,
            "project": os.environ.get(PROJECT_ENV) or None,
            "environment": os.environ.get(ENVIRONMENT_ENV) or None,
            "credentialSource": SDK_KEY_ENV,
            "note": (
                "LaunchDarkly flags are provisioned server-side; an SDK key cannot create "
                "them. This manifest declares what the run expects to be served and is not "
                "itself a flag definition file."
            ),
            "flags": list(flags.values()),
        }

        path = self._manifest_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return path

    def evaluate(self, key: str, ctx: dict[str, Any]) -> FlagResult:
        """Evaluate one flag through the OpenFeature client.

        ``ctx`` is an OpenFeature evaluation context plus two ADLC conveniences:

        ``default``
            The default value; its Python type selects the typed OpenFeature
            accessor (bool → ``get_boolean_details`` and so on). Defaults to
            ``False``.
        ``flagSetId``
            Recorded on the emitted telemetry as ``feature_flag.set.id``.

        Never raises: an evaluation error is returned as a ``FlagResult`` with
        ``reason`` set to ``ERROR``, because a flag backend outage must not fail
        a build.
        """
        context = dict(ctx or {})
        default = context.pop("default", False)
        flag_set_id = context.pop("flagSetId", None)
        targeting_key = context.get("targetingKey") or context.get("key")

        try:
            client = self._openfeature_client()
            details = self._details(client, key, default, context)
            result: dict[str, Any] = {
                "key": key,
                "value": getattr(details, "value", default),
                "variant": getattr(details, "variant", None),
                "reason": _reason_str(getattr(details, "reason", None)),
            }
        except Exception as exc:  # noqa: BLE001 - a flag outage is not a build failure
            _LOG.warning("LaunchDarkly evaluation of '%s' failed: %s", key, exc)
            result = {"key": key, "value": default, "variant": None, "reason": "ERROR"}

        self._emit_evaluation(result, targeting_key, flag_set_id)
        return result  # type: ignore[return-value]

    # -- telemetry --------------------------------------------------------

    def evaluation_attributes(
        self,
        result: Mapping[str, Any],
        *,
        context_id: str | None = None,
        flag_set_id: str | None = None,
    ) -> dict[str, Any]:
        """OTel attributes for one evaluation, using current semconv names.

        Delegates to :func:`adlc.stages.experiment.flag_evaluation_attributes` so
        every provider emits identical attribute keys: ``feature_flag.key``,
        ``feature_flag.provider.name`` (dotted — the ``provider_name`` spelling is
        obsolete), ``feature_flag.result.variant``, ``feature_flag.result.value``,
        ``feature_flag.result.reason``, ``feature_flag.context.id`` and
        ``feature_flag.set.id``.
        """
        return flag_evaluation_attributes(
            str(result.get("key")),
            provider_name=self.name,
            value=result.get("value"),
            variant=result.get("variant"),
            reason=result.get("reason"),
            context_id=context_id,
            flag_set_id=flag_set_id,
        )

    def _emit_evaluation(
        self, result: Mapping[str, Any], context_id: Any, flag_set_id: Any
    ) -> None:
        if self._telemetry is None:
            return
        attributes = self.evaluation_attributes(
            result,
            context_id=str(context_id) if context_id else None,
            flag_set_id=str(flag_set_id) if flag_set_id else None,
        )
        try:
            self._telemetry.emit({"name": "feature_flag.evaluation", "attributes": attributes})
        except Exception:  # noqa: BLE001 - telemetry is never load-bearing
            _LOG.debug("telemetry emit failed for flag '%s'", result.get("key"), exc_info=True)

    # -- internals --------------------------------------------------------

    def _manifest_path(self, run_id: str) -> Path:
        if self._run_dir is not None:
            return self._run_dir / MANIFEST_NAME
        override = os.environ.get("ADLC_RUN_DIR")
        if override:
            return Path(override) / MANIFEST_NAME
        from adlc.config import Config as _Config

        return Path(_Config.load().run_dir(run_id)) / MANIFEST_NAME

    def _openfeature_client(self) -> Any:
        """Bind the OpenFeature LaunchDarkly provider once, then reuse the client.

        Imported lazily: the module must remain importable — and the adapter
        discoverable — on a machine with no LaunchDarkly packages installed.
        """
        if self._client is not None:
            return self._client

        sdk_key = (os.environ.get(SDK_KEY_ENV) or "").strip()
        if not sdk_key:
            raise RuntimeError(f"{SDK_KEY_ENV} is not set")

        from ld_openfeature import Config as LdConfig
        from ld_openfeature import LaunchDarklyProvider as OpenFeatureLaunchDarklyProvider
        from openfeature import api

        api.set_provider(OpenFeatureLaunchDarklyProvider(LdConfig(sdk_key)), OPENFEATURE_DOMAIN)
        self._client = api.get_client(OPENFEATURE_DOMAIN)
        return self._client

    @staticmethod
    def _details(client: Any, key: str, default: Any, context: Mapping[str, Any]) -> Any:
        """Call the typed OpenFeature accessor matching ``default``'s type."""
        evaluation_context = _evaluation_context(context)
        if isinstance(default, bool):
            accessor = client.get_boolean_details
        elif isinstance(default, str):
            accessor = client.get_string_details
        elif isinstance(default, int):
            accessor = client.get_integer_details
        elif isinstance(default, float):
            accessor = client.get_float_details
        else:
            accessor = client.get_object_details
        return accessor(key, default, evaluation_context)


def _evaluation_context(context: Mapping[str, Any]) -> Any:
    """Build an OpenFeature ``EvaluationContext``, tolerating an absent SDK."""
    attributes = {k: v for k, v in context.items() if k not in ("targetingKey", "key")}
    targeting_key = context.get("targetingKey") or context.get("key")
    try:
        from openfeature.evaluation_context import EvaluationContext
    except ImportError:
        return None
    return EvaluationContext(
        targeting_key=str(targeting_key) if targeting_key else None,
        attributes=attributes,
    )


def _reason_str(reason: Any) -> str | None:
    """OpenFeature reasons are an enum in some versions and a string in others."""
    if reason is None:
        return None
    return str(getattr(reason, "value", reason))
