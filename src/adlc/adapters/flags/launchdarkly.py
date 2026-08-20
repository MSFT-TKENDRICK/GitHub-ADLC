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

#: Overrides the run directory a provider writes into when it was constructed
#: without an explicit path. Needed because the frozen ``materialize(run)``
#: signature carries no run directory, and a bare ``.adlc/...`` default is
#: resolved against the **process cwd** — which is not the repo root in a
#: container, an Actions job with a custom ``working-directory``, or any caller
#: that chdir'd. See ``docs/experiments.md`` §8.
RUN_DIR_ENV = "ADLC_RUN_DIR"


def default_manifest_path(run_id: str, filename: str) -> Path:
    """Resolve where a flag provider should write when given no explicit path.

    Ordering: ``ADLC_RUN_DIR`` → the configured run directory → a cwd-relative
    default. Only the last is cwd-dependent, and it is reached only when there is
    no config to consult at all.
    """
    override = (os.environ.get(RUN_DIR_ENV) or "").strip()
    if override:
        return Path(override) / filename
    try:
        from adlc.config import Config

        return Path(Config.load().run_dir(run_id)) / filename
    except Exception:  # noqa: BLE001 - never fail materialize() over path discovery
        return Path(".adlc") / "runs" / run_id / filename


class LaunchDarklyProvider:
    """Deliver ADLC experiment flags via LaunchDarkly through OpenFeature.

    Registered as the ``launchdarkly`` entry point in ``adlc.flags``.
    """

    name = "launchdarkly"
    kind = "flags"

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        telemetry: Any = None,
        client: Any = None,
    ) -> None:
        #: Where :meth:`materialize` writes the manifest. Same meaning as
        #: ``FlagdFileProvider.path``, so the two providers are interchangeable.
        self.path = Path(path) if path else None
        self._telemetry = telemetry
        self._client = client
        self._flag_set_id: str | None = None

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

    def materialize(self, run: Run) -> Path:
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
            "flagSetId": f"adlc/{run_id}",
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

        target = self.path or default_manifest_path(run_id, MANIFEST_NAME)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        self.path = target
        self._flag_set_id = str(manifest["flagSetId"])
        return target

    def evaluate(self, key: str, ctx: dict[str, Any]) -> FlagResult:
        """Evaluate one flag through the OpenFeature client.

        ``ctx`` is an OpenFeature evaluation context plus two ADLC conveniences:

        ``default``
            The default value; its Python type selects the typed OpenFeature
            accessor (bool → ``get_boolean_details`` and so on). Defaults to
            ``False``.
        ``flagSetId``
            Recorded on the emitted telemetry as ``feature_flag.set.id``; falls
            back to the flag set named by the last :meth:`materialize` call.

        Neither is forwarded to LaunchDarkly as a context attribute.

        Never raises: an evaluation error is returned as a ``FlagResult`` with
        ``reason`` set to ``ERROR``, because a flag backend outage must not fail
        a build.
        """
        context = dict(ctx or {})
        default = context.pop("default", False)

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

        self._emit_evaluation(result, ctx or {})
        return result  # type: ignore[return-value]

    # -- telemetry --------------------------------------------------------

    def span_attributes(self, result: FlagResult, ctx: dict[str, Any]) -> dict[str, Any]:
        """OTel feature-flag semconv attributes for this evaluation.

        Same signature and same output shape as
        ``FlagdFileProvider.span_attributes``, so a caller can emit spans for
        either provider without special-casing. Delegates to
        :func:`adlc.stages.experiment.flag_evaluation_attributes` so the attribute
        names have exactly one definition: ``feature_flag.key``,
        ``feature_flag.provider.name`` (dotted — the ``provider_name`` spelling is
        obsolete), ``feature_flag.result.variant`` / ``.value`` / ``.reason``,
        ``feature_flag.context.id`` and ``feature_flag.set.id``.
        """
        return flag_evaluation_attributes(
            str(result.get("key")),
            provider_name=self.name,
            value=result.get("value"),
            variant=result.get("variant"),
            reason=result.get("reason"),
            context_id=ctx.get("targetingKey") or ctx.get("id") or ctx.get("key"),
            flag_set_id=ctx.get("flagSetId") or self._flag_set_id,
        )

    def _emit_evaluation(self, result: Mapping[str, Any], ctx: Mapping[str, Any]) -> None:
        if self._telemetry is None:
            return
        attributes = self.span_attributes(result, dict(ctx))  # type: ignore[arg-type]
        builder = getattr(self._telemetry, "emit_flag_evaluation", None)
        try:
            if builder is not None:
                builder(
                    key=str(result.get("key")),
                    variant=result.get("variant") or "",
                    value=result.get("value"),
                    reason=str(result.get("reason") or ""),
                    provider=self.name,
                    context_id=attributes.get("feature_flag.context.id"),
                    flag_set_id=attributes.get("feature_flag.set.id"),
                )
            else:
                self._telemetry.emit({"name": "feature_flag.evaluation", **attributes})
        except Exception:  # noqa: BLE001 - telemetry is never load-bearing
            _LOG.debug("telemetry emit failed for flag '%s'", result.get("key"), exc_info=True)

    # -- internals --------------------------------------------------------

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


#: Context keys that are ADLC conveniences or the targeting key itself. None of
#: them may be forwarded to LaunchDarkly as a custom context attribute.
RESERVED_CONTEXT_KEYS = ("targetingKey", "key", "default", "flagSetId")


def _context_attributes(context: Mapping[str, Any]) -> dict[str, Any]:
    """Custom attributes to send, with ADLC-only keys stripped."""
    return {k: v for k, v in context.items() if k not in RESERVED_CONTEXT_KEYS}


def _evaluation_context(context: Mapping[str, Any]) -> Any:
    """Build an OpenFeature ``EvaluationContext``, tolerating an absent SDK."""
    targeting_key = context.get("targetingKey") or context.get("key")
    try:
        from openfeature.evaluation_context import EvaluationContext
    except ImportError:
        return None
    return EvaluationContext(
        targeting_key=str(targeting_key) if targeting_key else None,
        attributes=_context_attributes(context),
    )


def _reason_str(reason: Any) -> str | None:
    """OpenFeature reasons are an enum in some versions and a string in others."""
    if reason is None:
        return None
    return str(getattr(reason, "value", reason))
