# Wiring Azure SRE Agent to dispatch an ADLC hotfix

> **DISABLED EXAMPLE — requires an Azure subscription.**
> Nothing in this directory is executed, applied or referenced by ADLC. With no
> Azure environment variables set, `adlc doctor` reports every adapter here as
> unavailable and the spine's credential-free defaults run instead. This
> document is a runbook for a human with a subscription, not automation.

---

## 1. What the SRE Agent can actually do

Verified 2026-08-19 against
[`/azure/sre-agent/github-connector`](https://learn.microsoft.com/en-us/azure/sre-agent/github-connector).
Quoted capabilities, verbatim from that page:

| Capability | Status | Docs say |
|---|---|---|
| Create issues | **VERIFIED** | "Create issues with title, body, labels, and assignees." |
| Update issues | **VERIFIED** | "Update issues by changing title, body, labels, or state." |
| Comment on issues and PRs | **VERIFIED** | "Comment on issues and pull requests including auto-close keywords." |
| Read Dependabot alerts | **VERIFIED** | "Fetch Dependabot alerts to review security vulnerabilities." |
| Trigger Actions workflows | **VERIFIED** | "Trigger GitHub Actions workflows to dispatch canary or production deployments." |
| Track workflow runs | **VERIFIED** | "Track workflow runs to monitor status of dispatched workflows." |

### The caveat we will not paper over

> **UNVERIFIED: whether the SRE Agent can autonomously author code and open a
> pull request.**

ADLC's design notes describe the SRE Agent as unable to open code PRs. **We could
not verify that.** What we found instead:

- The connector page lists "open/merge PRs" under pull-request operations.
- The overview page says *"The agent proposes changes and your team approves.
  No change deploys without human sign-off."*
- **No page states** whether the agent generates code diffs itself, versus
  opening/merging/commenting on pull requests that already exist.
- We found **no page asserting it cannot**, either.

Pages checked: `sre-agent/github-connector`, `sre-agent/setup-github-connector`,
`sre-agent/overview`, `sre-agent/create-and-set-up`.

**So do not repeat "the SRE Agent cannot open PRs" as fact.** Say instead: *it is
not documented to generate code fixes, and we did not design around it doing so.*

### Why the dispatch design is right regardless

This matters less than it looks, because the architecture does not depend on the
answer. Even if the SRE Agent *could* open a code PR, we would still route
through `repository_dispatch`, because a PR authored outside ADLC arrives with:

- no run directory, no `taskgraph.json`, no immutable stage results,
- no captured evidence and therefore nothing for `evidence_completeness` to check,
- no rubric scores, no ADR, no `referencesRun` lineage.

It would be a change that skipped every gate the framework exists to enforce.
Dispatching a workflow is the capability that lets an incident enter the
**governed** path — so we use the capability that is both documented *and*
architecturally correct.

---

## 2. Onboarding the agent

**There is no `az` CLI, ARM or Bicep provisioning path for the SRE Agent itself.**
We searched every SRE Agent page listed above and found none; onboarding is the
portal wizard.

From [`/azure/sre-agent/create-and-set-up`](https://learn.microsoft.com/en-us/azure/sre-agent/create-and-set-up):

> "Go to the Azure SRE Agent webpage at **sre.azure.com**. Sign in with your
> Azure credentials. Select Basics > Review > Deploy to open the wizard."

Prerequisites documented on that page:

- **Contributor** on the subscription — to register resource providers and create resources.
- **Owner** or **User Access Administrator** — to create role assignments.
- The wizard creates a **managed identity** and its role assignments for you.

> **UNVERIFIED:** the ARM **resource provider namespace** for the SRE Agent
> (e.g. `Microsoft.SreAgent`). Not stated on any page we read. Do not guess it.

---

## 3. RBAC

### Grant the agent read access to your resources

Documented on `create-and-set-up`:

> "Granting the agent **Reader** access to your Azure resources allows it to
> query metrics, logs, and resource configurations during investigations."

`Reader` is a built-in role: GUID `acdd72a7-3385-48ef-bd42-f606fba81ae7`.

The exact `az role assignment create` invocation below follows the
[CLI reference](https://learn.microsoft.com/en-us/cli/azure/role/assignment).
Required parameters are `--role` and `--scope`.

```bash
# Look up the agent's managed identity principal id in the portal first.
AGENT_PRINCIPAL_ID="<object-id-of-the-sre-agent-managed-identity>"
SUBSCRIPTION_ID="<your-subscription-id>"
RESOURCE_GROUP="<your-resource-group>"

az role assignment create \
  --assignee-object-id "$AGENT_PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role Reader \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP"
```

`--assignee-object-id` + `--assignee-principal-type` is preferred over
`--assignee` for a managed identity. The CLI docs say `--assignee-object-id`
exists to *"bypass Microsoft Graph query in case the logged-in account has no
permission or the machine has no network access"*, and `--assignee-principal-type`
is *"use with `--assignee-object-id` to avoid errors caused by propagation
latency in Microsoft Graph."*

> **UNVERIFIED:** the exact set of roles the wizard assigns to the agent's own
> managed identity. `create-and-set-up` says only that it "grants the managed
> identity required access" without enumerating roles. There is no documented
> `az role assignment create` command for SRE Agent setup itself — the commands
> here are for *your* resources, using the general CLI reference.

### If you also deploy the git-mirror container app

`container-app-with-git-mirror.bicep` outputs `principalId`. Grant it `AcrPull`
(GUID `7f951dda-4ed3-4680-a7ca-43fe172d538d`) on your registry:

```bash
az role assignment create \
  --assignee-object-id "$(az deployment group show -g "$RESOURCE_GROUP" \
      -n container-app-with-git-mirror --query properties.outputs.principalId.value -o tsv)" \
  --assignee-principal-type ServicePrincipal \
  --role AcrPull \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.ContainerRegistry/registries/<registry-name>"
```

Note: for registries in "RBAC Registry + ABAC Repository Permissions" mode,
Microsoft now recommends the more fine-grained
`Container Registry Repository Reader` for pull-only scenarios. `AcrPull`
remains a valid documented built-in role.

### Foundry roles

If you also run `foundry-hotfix-agent.yaml`, note that the built-in role
`Azure AI Developer` (GUID `64702f94-c441-49e6-a78b-ef80e0188fee`) carries this
description verbatim:

> "…For Foundry project access, use the **Foundry User** or **Foundry Owner**
> roles instead."

So `Azure AI Developer` is *not* the right role for a Foundry project.

> **UNVERIFIED:** the role definition GUIDs for `Foundry User` / `Foundry Owner`.
> The names are documented; we did not find their GUIDs. Assign them by name.

---

## 4. Connect the GitHub connector

Follow [`/azure/sre-agent/setup-github-connector`](https://learn.microsoft.com/en-us/azure/sre-agent/setup-github-connector)
(OAuth or PAT). Grant the connector access to the repository that hosts your ADLC
configuration.

The token needs enough scope to **dispatch a workflow** and **create issues**.
Both intake paths in §5 are supported; you only need one.

---

## 5. Two intake paths — pick one

ADLC accepts either. Both end in the same place: a `brief.md` that enters the
**ordinary day-1 intake path**.

### Path A — `repository_dispatch` (fast)

Configure the SRE Agent to POST a `repository_dispatch` with event type
`adlc-incident`. The `client_payload` is parsed by
`adlc.adapters.daytwo.sre_agent.SreAgentReceiver`.

```jsonc
{
  "event_type": "adlc-incident",
  "client_payload": {
    "id": "INC-2026-08-19-0007",
    "title": "Checkout p95 latency breached SLO after deploy",
    "severity": "sev2",
    "detectedAt": "2026-08-19T14:07:00Z",
    "summary": "p95 latency on POST /api/checkout rose from 380ms to 2.4s.",
    "impact": "Roughly 8% of checkout attempts time out at the client.",
    "suspectedCause": "New synchronous inventory lookup added in 9f2c1ab.",
    "resource": {
      "id": "/subscriptions/.../providers/Microsoft.App/containerApps/adlc-day2-demo",
      "name": "adlc-day2-demo",
      "type": "Microsoft.App/containerApps",
      "resourceGroup": "rg-adlc-demo",
      "region": "eastus"
    },
    "deployment": { "commit": "9f2c1ab...", "environment": "production" },
    "signals": [
      {
        "id": "S001",
        "kind": "metric",
        "description": "p95 latency, POST /api/checkout",
        "value": 2400, "threshold": 800, "unit": "ms",
        "query": "AppRequests | where Name == 'POST /api/checkout' | summarize percentile(DurationMs, 95) by bin(TimeGenerated, 5m)"
      }
    ],
    "links": [{ "title": "Incident in SRE Agent", "url": "https://sre.azure.com/..." }]
  }
}
```

Every field is optional. `SreAgentReceiver` normalises common alternative
spellings (`alertName`, `firedAt`, `resourceId`, `probableCause`, `sev`,
`priority`, …) and preserves the **entire** inbound payload under
`incident["raw"]` so the audit trail is complete.

### Path B — an issue the agent files (most robust)

Creating an issue is the SRE Agent's best-verified capability. Configure it to
open an issue labelled `adlc:incident`, and optionally embed the structured
payload in a fenced ` ```json ` block in the body — the receiver extracts it and
falls back to the issue title/body when it is absent. Severity is inferred from
labels (`sev2`, `severity: high`, `critical`, …).

---

## 6. The receiving workflow

> **This is a template to copy, not an active workflow.** ADLC's own
> `.github/workflows/` is owned by the spine; this file lives under `examples/`
> so it is inert. Copy it into your consumer repo's `.github/workflows/`.

```yaml
# .github/workflows/adlc-hotfix.yml  (copy into YOUR repo)
name: ADLC hotfix
on:
  repository_dispatch:
    types: [adlc-incident]
  issues:
    types: [opened, labeled]
  workflow_dispatch:
    inputs:
      incident:
        description: JSON incident payload
        required: true

permissions:
  contents: write
  pull-requests: write
  issues: read

jobs:
  hotfix:
    # Only act on incident issues, never on every issue in the repo.
    if: >-
      github.event_name != 'issues' ||
      contains(github.event.issue.labels.*.name, 'adlc:incident')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install adlc

      # GITHUB_EVENT_PATH is set automatically; SreAgentReceiver.detect() finds
      # it and the receiver parses whichever event shape arrived.
      - name: Incident -> brief -> narrow run -> gates
        run: adlc hotfix --json
```

`adlc hotfix` **fails closed**: if the required gates did not actually run, it
exits non-zero rather than reporting success. Pass `--allow-incomplete` only if
you understand you are accepting an ungated result.

---

## 7. What is real and what is not

| Thing | Status |
|---|---|
| `SreAgentReceiver` payload parsing | **Real.** Credential-free, unit-tested in `tests/l10_daytwo`. |
| `adlc hotfix` incident → brief → narrow graph | **Real.** Credential-free, unit-tested. |
| Reuse of the day-1 intake path | **Real.** Same `brief.md`, same `adlc run new`. |
| SRE Agent creating the incident | **Example.** Needs a subscription and the portal wizard. |
| The Bicep container app | **Example.** Never applied by ADLC; not Bicep-compiled in CI. |
| The Foundry hosted agent | **Example.** Also needs an HTTP protocol shim ADLC does not ship. |
| App Insights telemetry export | **Example.** Reports unavailable without a connection string. |

See [`docs/day2-operations.md`](../../docs/day2-operations.md) for the full loop.
