"""The ``examples/azure/`` manifests must be syntactically valid and clearly disabled.

**What these tests prove, and what they do not.**

* The YAML test is a *real* syntax check — ``yaml.safe_load`` either parses it
  or it does not.
* The Bicep and KQL tests are **structural only**. This suite is credential-free
  and has no Bicep compiler or Kusto parser, so they check delimiter balance and
  the presence of field names we verified against Microsoft's published schema.
  They do **not** prove ``bicep build`` succeeds or that the KQL runs.
  Saying so here is the point: an over-claimed test is worse than a modest one.
"""

from __future__ import annotations

import re

import pytest
import yaml

from adlc.adapters.daytwo.foundry import VALID_PROTOCOLS, FoundryHotfixAgent
from tests.l10_daytwo.conftest import EXAMPLES

BICEP = EXAMPLES / "container-app-with-git-mirror.bicep"
FOUNDRY_YAML = EXAMPLES / "foundry-hotfix-agent.yaml"
DISPATCH_MD = EXAMPLES / "sre-agent-dispatch.md"
KQL = EXAMPLES / "continuous-eval.kql"
ALL_EXAMPLES = [BICEP, FOUNDRY_YAML, DISPATCH_MD, KQL]
EXAMPLE_IDS = [p.name for p in ALL_EXAMPLES]


@pytest.mark.parametrize("path", ALL_EXAMPLES, ids=EXAMPLE_IDS)
def test_example_exists_and_is_not_empty(path) -> None:
    assert path.is_file(), f"{path} is missing"
    assert path.stat().st_size > 0


@pytest.mark.parametrize("path", ALL_EXAMPLES, ids=EXAMPLE_IDS)
def test_every_example_declares_itself_disabled(path) -> None:
    """Each file must say, near the top, that it is a disabled example."""
    head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:12]).upper()
    assert "DISABLED" in head, f"{path.name} does not declare itself disabled"
    assert "AZURE SUBSCRIPTION" in head, f"{path.name} does not state it needs a subscription"


# -- YAML: a genuine syntax check -------------------------------------------


def test_foundry_yaml_parses() -> None:
    document = yaml.safe_load(FOUNDRY_YAML.read_text(encoding="utf-8"))
    assert isinstance(document, dict)


def test_foundry_yaml_uses_the_verified_azure_yaml_schema() -> None:
    """Field names verified against /azure/foundry/agents/concepts/azure-yaml-reference."""
    document = yaml.safe_load(FOUNDRY_YAML.read_text(encoding="utf-8"))

    # Top level is `name` + `services`; there is no top-level `agents:`/`ai:`.
    assert set(document) == {"name", "services"}
    assert "agents" not in document and "ai" not in document

    agent = document["services"]["adlc-hotfix"]
    assert agent["host"] == "azure.ai.agent"
    assert agent["kind"] == "hosted"
    assert agent["language"] == "docker"
    assert isinstance(agent["env"], dict)          # `env` is a flat map, not an array
    assert agent["container"]["resources"]["cpu"] == "1.0"
    assert agent["container"]["resources"]["memory"] == "2.0Gi"

    # protocols is an array of {protocol, version} with a documented protocol value
    assert agent["protocols"][0]["protocol"] in VALID_PROTOCOLS
    assert "version" in agent["protocols"][0]


def test_foundry_yaml_does_not_declare_the_platform_injected_endpoint() -> None:
    """The docs warn that declaring FOUNDRY_PROJECT_ENDPOINT shadows the platform value."""
    agent = yaml.safe_load(FOUNDRY_YAML.read_text(encoding="utf-8"))["services"]["adlc-hotfix"]
    assert "FOUNDRY_PROJECT_ENDPOINT" not in agent["env"]
    assert not any(key.startswith(("FOUNDRY_", "AGENT_")) for key in agent["env"])


def test_foundry_yaml_matches_what_the_adapter_renders() -> None:
    """The committed example must not drift from the adapter that generates it."""
    assert FOUNDRY_YAML.read_text(encoding="utf-8") == FoundryHotfixAgent().render_yaml()


def test_foundry_yaml_carries_the_honesty_caveats() -> None:
    text = FOUNDRY_YAML.read_text(encoding="utf-8")
    assert "UNVERIFIED" in text
    assert "SHIM NOT SHIPPED" in text
    assert "adlc hotfix --incident" in text


def test_adapter_rejects_an_undocumented_protocol() -> None:
    with pytest.raises(ValueError, match="not one of the documented values"):
        FoundryHotfixAgent().agent_service(protocol="totally-made-up")


# -- Bicep: structural only --------------------------------------------------


def _balanced(text: str, opener: str, closer: str) -> bool:
    depth = 0
    for char in text:
        depth += char == opener
        depth -= char == closer
        if depth < 0:
            return False
    return depth == 0


def test_bicep_delimiters_balance() -> None:
    """Not a compile. Just: nobody left a brace open."""
    body = "\n".join(
        line for line in BICEP.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("//")
    )
    assert _balanced(body, "{", "}"), "unbalanced braces"
    assert _balanced(body, "[", "]"), "unbalanced brackets"
    assert _balanced(body, "(", ")"), "unbalanced parentheses"


def test_bicep_declares_a_resource_and_its_parameters() -> None:
    text = BICEP.read_text(encoding="utf-8")
    assert re.search(r"^resource \w+ 'Microsoft\.App/containerApps@[\d-]+' = \{", text,
                     re.MULTILINE)
    for param in ("environmentId", "appImage", "gitMirrorImage", "repoUrl", "deployedCommit"):
        assert re.search(rf"^param {param} ", text, re.MULTILINE), f"missing param {param}"


def test_bicep_uses_verified_container_apps_field_names() -> None:
    """Every name here was checked against the Microsoft.App/containerApps reference."""
    text = BICEP.read_text(encoding="utf-8")
    for field in ("environmentId:", "template:", "containers:", "initContainers:",
                  "volumes:", "volumeMounts:", "volumeName:", "mountPath:",
                  "storageType:", "resources:", "ingress:", "targetPort:", "transport:"):
        assert field in text, f"missing documented field {field}"


def test_bicep_uses_the_documented_emptydir_casing() -> None:
    text = BICEP.read_text(encoding="utf-8")
    assert "storageType: 'EmptyDir'" in text
    # storageName is documented as unnecessary for EmptyDir - don't emit it.
    # Comments are excluded because they explain exactly that.
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )
    assert "storageName" not in code


def test_bicep_avoids_the_deprecated_environment_property() -> None:
    assert "managedEnvironmentId:" not in BICEP.read_text(encoding="utf-8")


def test_bicep_respects_the_verified_consumption_cpu_memory_ratio() -> None:
    """Consumption plan is a fixed 1 vCPU : 2 GiB, summed across all containers."""
    text = BICEP.read_text(encoding="utf-8")
    cpus = [float(v) for v in re.findall(r"cpu: json\('([\d.]+)'\)", text)]
    memories = [float(v) for v in re.findall(r"memory: '([\d.]+)Gi'", text)]
    assert len(cpus) == len(memories) == 3     # init + app + sidecar

    # Init containers run before app containers, so the concurrent total is the
    # two app containers only.
    app_cpu, app_memory = sum(cpus[1:]), sum(memories[1:])
    assert app_cpu == pytest.approx(1.0)
    assert app_memory == pytest.approx(2.0)
    assert app_memory == pytest.approx(app_cpu * 2), "violates the documented 1 vCPU : 2 GiB ratio"
    assert app_cpu <= 4.0 and app_memory <= 8.0
    for cpu in cpus:
        assert (cpu * 100) % 25 == 0, f"{cpu} is not a multiple of 0.25 vCPU"


def test_bicep_documents_what_it_could_not_verify() -> None:
    text = BICEP.read_text(encoding="utf-8")
    assert "UNVERIFIED" in text
    assert "NOT VALIDATED BY CI" in text


# -- KQL: structural only ----------------------------------------------------


def test_kql_delimiters_balance() -> None:
    body = "\n".join(
        line for line in KQL.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("//")
    )
    assert _balanced(body, "(", ")"), "unbalanced parentheses"
    assert _balanced(body, "[", "]"), "unbalanced brackets"


def test_kql_uses_the_verified_workspace_table_names() -> None:
    text = KQL.read_text(encoding="utf-8")
    for table in ("AppRequests", "AppDependencies", "AppTraces", "AppEvents"):
        assert table in text


def test_kql_uses_current_semconv_attribute_spellings() -> None:
    text = KQL.read_text(encoding="utf-8")
    for attribute in ("feature_flag.key", "feature_flag.provider.name",
                      "feature_flag.result.variant", "feature_flag.result.reason",
                      "feature_flag.context.id", "feature_flag.set.id",
                      "gen_ai.provider.name", "gen_ai.usage.input_tokens"):
        assert attribute in text, f"missing {attribute}"
    # Superseded spellings must not be used as the primary lookup.
    assert 'Properties["feature_flag.variant"]' not in text
    assert 'Properties["feature_flag.provider_name"]' not in text


def test_kql_flags_the_unverified_foundry_eval_schema() -> None:
    text = KQL.read_text(encoding="utf-8")
    assert "UNVERIFIED" in text
    assert "Query 0" in text, "there must be a discovery query before assuming a schema"


# -- the runbook -------------------------------------------------------------


def test_dispatch_runbook_states_the_unverified_pr_claim() -> None:
    text = DISPATCH_MD.read_text(encoding="utf-8")
    assert "UNVERIFIED" in text
    assert "sre.azure.com" in text
    assert "az role assignment create" in text
    # It must not assert the unproven negative as fact.
    assert "do not repeat" in text.lower()
