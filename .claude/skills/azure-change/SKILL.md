---
name: azure-change
description: Protocol for every action that writes to Azure (from M3 on). Use whenever a deployment, a resource or a setting in Azure would change.
---
# Azure change

1. Say in one sentence what will change and which resource it touches.
2. Dry run first: `az deployment group what-if -g <rg> -f infra/main.bicep -p infra/main.bicepparam` with the same parameters the real run would use. Placeholders for secrets only when the scope cannot reach the app (`scope=shared`); never deploy a placeholder.
3. Show the raw what-if output, unabridged, including Ignore and NoChange lines. Do not summarise it away.
4. Wait for Timur's yes. No yes, no change.
5. After the yes: Timur starts the deployment (the deploy workflow in the protected environment `production`, or the create by hand). You never run `az deployment group create` (denied). Then read the proof (provisioning state, revision, image tag, secret **names** only).
6. Roles, identities, federated credentials, environments and permissions are never changed by the agent. List them as a table (what, scope, why) for Timur.
7. Report what changed, what the what-if predicted, and any difference between the two.
