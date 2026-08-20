// =============================================================================
// DISABLED EXAMPLE - NOT DEPLOYED BY ADLC.
//
// Requires an Azure subscription. ADLC never reads, compiles or applies this
// file; no ADLC code path references it. It exists so the day-2 story in
// docs/day2-operations.md is concrete rather than hand-waved.
//
// To use it you must supply your own container images and deploy it yourself:
//   az deployment group create -g <rg> -f container-app-with-git-mirror.bicep \
//     -p environmentId=<id> appImage=<img> gitMirrorImage=<img> repoUrl=<url>
//
// NOT VALIDATED BY CI. The ADLC test suite is credential-free and has no Bicep
// toolchain, so tests/l10_daytwo asserts this file's *structure* and that it
// uses the documented field names - it does NOT prove `bicep build` succeeds.
// Run `az bicep build --file container-app-with-git-mirror.bicep` yourself.
// =============================================================================
//
// WHAT THIS IS
// ------------
// An Azure Container App running two containers in one replica:
//
//   1. `app`         - your application.
//   2. `git-mirror`  - a sidecar holding a git mirror of the repo at the
//                      deployed commit, on a volume the app container can read.
//
// WHY a git mirror helps day-2: when the SRE Agent raises an incident, the
// first question is always "what source is actually running right now?".
// A mirror pinned to the deployed commit answers that from inside the replica,
// so the incident payload can carry an exact SHA instead of a guess. That SHA
// becomes `deployment.commit` in the ADLC incident, and `baseSha` in the hotfix
// task graph.
//
// IMPORTANT: this sidecar does NOT hot-patch the running app. Nothing here
// writes to production. It is a read-only provenance aid. The fix itself goes
// through the normal ADLC pipeline and a normal pull request.
//
// VERIFIED CONSTRAINTS (learn.microsoft.com, checked 2026-08-19)
// --------------------------------------------------------------
// Source: /azure/container-apps/containers and /azure/container-apps/storage-mounts
// Source: /azure/templates/microsoft.app/containerapps?pivots=deployment-language-bicep
//
// * Containers in one container app "share hard disk and network resources and
//   experience the same application lifecycle" - so they reach each other over
//   localhost and can share a volume.
// * `EmptyDir` is REPLICA-scoped: "Files persist for the lifetime of the
//   replica. If a container in a replica restarts, the files in the volume
//   remain." and "Any init or app containers in the replica can mount the same
//   volume." Exact enum casing is 'EmptyDir'.
// * `storageName` is not needed for EmptyDir ("No need to provide for EmptyDir
//   and Secret.").
// * Consumption plan CPU:memory is a FIXED 1 vCPU : 2 GiB ratio. Allowed values
//   run 0.25/0.5Gi .. 4.0/8.0Gi in 0.25 vCPU steps. These totals are across ALL
//   containers in the replica combined - main + sidecars.
//   The two containers below sum to exactly 1.0 vCPU / 2.0Gi.
// * A "Consumption only" environment is capped at 2 cores and 4Gi.
// * Ephemeral storage per replica by vCPU: <=0.25 -> 1GiB, <=0.5 -> 2GiB,
//   <=1 -> 4GiB, >1 -> 8GiB.
// * ACA does NOT support mounting Azure NetApp Files or Azure Blob Storage.
// * `properties.environmentId` is current; `properties.managedEnvironmentId` is
//   documented as deprecated.
// * apiVersion 2026-01-01 is the latest stable listed for
//   Microsoft.App/containerApps.
//
// UNVERIFIED
// ----------
// * No Microsoft-published "git mirror sidecar" image exists that we could
//   find, so `gitMirrorImage` has NO default - you must supply an image that
//   contains `git` and a POSIX shell. We will not invent an image reference.
// * ACA `volumeMounts` has no documented read-only flag, so "the app must not
//   write to the mirror" is a convention, not a platform guarantee.
// =============================================================================

targetScope = 'resourceGroup'

@description('Name of the container app.')
param appName string = 'adlc-day2-demo'

@description('Azure region. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Resource ID of an existing Managed Environment (Microsoft.App/managedEnvironments).')
param environmentId string

@description('Your application image, e.g. myacr.azurecr.io/app:sha-abc1234.')
param appImage string

@description('Image containing git and a POSIX shell. No default: supply your own. See UNVERIFIED note above.')
param gitMirrorImage string

@description('HTTPS clone URL of the repository to mirror.')
param repoUrl string

@description('Exact commit SHA that is deployed. This is the provenance anchor the incident payload carries.')
param deployedCommit string

@description('Seconds between mirror refreshes.')
param mirrorIntervalSeconds int = 300

@description('Container port the application listens on.')
param targetPort int = 8080

// Replica-scoped ephemeral volume shared by both containers.
var mirrorVolumeName = 'git-mirror'
var mirrorMountPath = '/srv/git-mirror'

resource containerApp 'Microsoft.App/containerApps@2026-01-01' = {
  name: appName
  location: location
  identity: {
    // A system-assigned identity is the least-privilege way to pull from ACR.
    // Grant it AcrPull separately - see examples/azure/sre-agent-dispatch.md.
    type: 'SystemAssigned'
  }
  properties: {
    environmentId: environmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: targetPort
        transport: 'http'
      }
    }
    template: {
      // Init container seeds the mirror once, before the app starts, so the
      // app never observes a half-populated volume.
      initContainers: [
        {
          name: 'git-mirror-init'
          image: gitMirrorImage
          command: [ '/bin/sh', '-c' ]
          args: [
            'set -eu; git clone --mirror "$REPO_URL" "$MIRROR_PATH/repo.git"; git --git-dir="$MIRROR_PATH/repo.git" cat-file -e "$DEPLOYED_COMMIT^{commit}"; echo "$DEPLOYED_COMMIT" > "$MIRROR_PATH/DEPLOYED_COMMIT"'
          ]
          env: [
            { name: 'REPO_URL', value: repoUrl }
            { name: 'MIRROR_PATH', value: mirrorMountPath }
            { name: 'DEPLOYED_COMMIT', value: deployedCommit }
          ]
          resources: {
            // The docs sample uses any('0.25') because Bicep's int type rejects
            // decimals; json('0.25') is the equivalent and is used here.
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          volumeMounts: [
            { volumeName: mirrorVolumeName, mountPath: mirrorMountPath }
          ]
        }
      ]
      containers: [
        {
          name: 'app'
          image: appImage
          env: [
            { name: 'ADLC_DEPLOYED_COMMIT', value: deployedCommit }
            { name: 'ADLC_GIT_MIRROR_PATH', value: mirrorMountPath }
          ]
          resources: {
            cpu: json('0.75')
            memory: '1.5Gi'
          }
          volumeMounts: [
            { volumeName: mirrorVolumeName, mountPath: mirrorMountPath }
          ]
        }
        {
          name: 'git-mirror'
          image: gitMirrorImage
          command: [ '/bin/sh', '-c' ]
          args: [
            'set -eu; while true; do git --git-dir="$MIRROR_PATH/repo.git" remote update --prune || echo "mirror refresh failed"; sleep "$MIRROR_INTERVAL"; done'
          ]
          env: [
            { name: 'MIRROR_PATH', value: mirrorMountPath }
            { name: 'MIRROR_INTERVAL', value: string(mirrorIntervalSeconds) }
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          volumeMounts: [
            { volumeName: mirrorVolumeName, mountPath: mirrorMountPath }
          ]
        }
      ]
      // Replica-scoped ephemeral volume. storageName is intentionally absent:
      // the docs state it is not needed for EmptyDir.
      volumes: [
        {
          name: mirrorVolumeName
          storageType: 'EmptyDir'
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

@description('Principal ID of the app system-assigned identity. Grant it AcrPull.')
output principalId string = containerApp.identity.principalId

@description('The commit this revision is pinned to. Carry it in the incident payload as deployment.commit.')
output deployedCommit string = deployedCommit

@description('Public FQDN of the app.')
output fqdn string = containerApp.properties.configuration.ingress.fqdn
