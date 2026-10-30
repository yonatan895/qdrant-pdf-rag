# Dev kit: the Windows operator path

The first push of the dev workspace image, and the creation of the bastion,
from the operator's Windows machine (no WSL) with `oc.exe` logged in to the
production namespace. Decisions: [ADR-0009 §8](../adr/0009-gap-fork.md#8-dev-workspace-image-and-dev-kit-d3).
This runbook ships inside every kit as `devkit-windows.md`.

You need:

- the kit directory `mfr-devkit-<key>\`, transferred as plain files (no
  archive, no links);
- the kit's **anchor**: 16 hex characters published on GitHub with the release
  record, carried to you by a route separate from the transfer;
- the release bundle's `sneakernet-signing.pub`, already checked against the
  release's own anchor;
- `oc.exe`, logged in (`oc login`) to the production namespace;
- the site values: the registry repository for the image, the registry robot
  Secret, the pull Secret, the storage class and the egress address lists.

Paste each block whole into **Windows PowerShell 5.1**. Each block stops at the
first failure with a `STOP:` line that names the fix; nothing after a STOP runs.
Blocks are safe to run again.

## Values

Edit the right-hand sides, then paste the block.

<!-- devkit-runbook: values -->
```powershell
$Kit          = 'D:\transfer\mfr-devkit-0000000000000000000000000000000000000000000000000000000000000000'
$Anchor       = '0000000000000000'                      # the 16 hex carried separately
$TrustedPub   = 'D:\transfer\release\sneakernet-signing.pub'
$Registry     = 'quay.example/mf/qdrant-pdf-rag-devkit' # repository only: no https://, tag or digest
$Robot        = 'builder-quay-mf-dev'                   # registry robot Secret (push access)
$ParamFile    = 'D:\transfer\bastion.env'               # outside the kit directory
$RegistryAuth = "$env:TEMP\mfr-devkit-registry-auth.json" # removed again at the end of step 2
$OrasCaFile   = ''                                      # the registry CA file, if Windows does not trust it
$InstallArgs  = @()                                     # e.g. @('--registry-ca-dir', '/work/registry-ca')
```

## 1. Verify the kit

`SHA256SUMS.sha256` catches corruption and the wrong version; the anchor catches
tampering. Both pass before anything is pushed and before any token reaches a
pod. The release key check catches a kit signed by another key.

<!-- devkit-runbook: step 1 -->
```powershell
. {
    $ErrorActionPreference = 'Stop'
    Set-Location -LiteralPath $Kit
    $fingerprint = (Get-FileHash -Algorithm SHA256 -LiteralPath .\SHA256SUMS).Hash.ToLowerInvariant()
    $recorded = ((Get-Content -LiteralPath .\SHA256SUMS.sha256 -TotalCount 1) -split ' ')[0]
    if ($fingerprint -ne $recorded) { throw 'STOP: SHA256SUMS does not match SHA256SUMS.sha256 (corrupt or wrong kit). Transfer the kit again.' }
    if ($fingerprint.Substring(0, 16) -ne $Anchor.Trim().ToLowerInvariant()) { throw 'STOP: the kit fingerprint does not match the anchor you carried. Do not use this kit; report it.' }
    foreach ($line in Get-Content -LiteralPath .\SHA256SUMS) {
        $want, $name = $line -split '  ', 2
        $path = Join-Path $Kit ($name -replace '/', '\')
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "STOP: kit member $name is missing. Transfer the kit again." }
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() -ne $want) { throw "STOP: kit member $name differs from SHA256SUMS. Transfer the kit again." }
    }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $TrustedPub).Hash -ne (Get-FileHash -Algorithm SHA256 -LiteralPath .\sneakernet-signing.pub).Hash) { throw 'STOP: the kit is not signed by the release key you verified. Do not use this kit; report it.' }
    "Kit verified: fingerprint $fingerprint"
}
```

## 2. Push the image with oras.exe

The image is an OCI layout directory, pushed by the digest the kit MANIFEST
names and read back from the registry. The robot credential is written to
`$RegistryAuth` only for this step.

<!-- devkit-runbook: step 2 -->
```powershell
. {
    $ErrorActionPreference = 'Stop'
    Set-Location -LiteralPath $Kit
    $key = ((Select-String -LiteralPath .\MANIFEST.txt -Pattern '^key: ').Line -split ' ')[1]
    $digest = ((Select-String -LiteralPath .\MANIFEST.txt -Pattern '^image_digest: ').Line -split ' ')[1]
    $copyTls = @(); $resolveTls = @()
    if ($OrasCaFile) { $copyTls = @('--to-ca-file', $OrasCaFile); $resolveTls = @('--ca-file', $OrasCaFile) }
    Unblock-File -LiteralPath .\oras.exe
    try {
        $b64 = oc get secret $Robot -o 'jsonpath={.data.\.dockerconfigjson}'
        if ($LASTEXITCODE -ne 0 -or -not $b64) { throw 'STOP: cannot read the registry robot Secret. Check $Robot and your oc login.' }
        [IO.File]::WriteAllBytes($RegistryAuth, [Convert]::FromBase64String($b64))
        # A relative layout path: oras would read a drive letter's colon as a tag.
        .\oras.exe cp --from-oci-layout "image@$digest" "${Registry}:$key" --to-registry-config $RegistryAuth @copyTls
        if ($LASTEXITCODE -ne 0) { throw 'STOP: oras.exe could not push. Check the registry address, its CA ($OrasCaFile) and the robot push access, or use the fallback below.' }
        $readback = .\oras.exe resolve --registry-config $RegistryAuth @resolveTls "${Registry}:$key"
        if ($LASTEXITCODE -ne 0 -or $readback -ne $digest) { throw "STOP: the registry reads back '$readback', not $digest. Do not create the bastion; report it." }
    } finally {
        Remove-Item -LiteralPath $RegistryAuth -ErrorAction SilentlyContinue
        Remove-Variable -Name b64 -ErrorAction SilentlyContinue
    }
    "Pushed ${Registry}:$key = $digest"
}
```

## 3. Create the bastion

Write the parameter file `$ParamFile` (outside the kit directory) in Notepad,
one `NAME=value` per line, and save it as **UTF-8 without a BOM** (Notepad:
*Save As*, *Encoding: UTF-8*). Do not create it with `>` or `Out-File` in
PowerShell 5.1: they write UTF-16, which `oc process` refuses, as it refuses a
BOM. `DEVKIT_DIGEST` is the kit's `image_digest`; the template's parameter
descriptions explain the rest:

```text
PROD_NAMESPACE=<production namespace>
DEVKIT_REPOSITORY=<the $Registry value>
DEVKIT_DIGEST=<image_digest from the kit MANIFEST.txt>
PULL_SECRET=<pull robot Secret>
STORAGE_CLASS=<RWO block storage class>
API_SERVER_CIDRS=[{"ipBlock":{"cidr":"<api server address>/32"}}]
REGISTRY_CIDRS=[{"ipBlock":{"cidr":"<registry address>/32"}}]
GITLAB_CIDRS=[{"ipBlock":{"cidr":"<gitlab address>/32"}}]
```

<!-- devkit-runbook: step 3 -->
```powershell
. {
    $ErrorActionPreference = 'Stop'
    $digest = ((Select-String -LiteralPath "$Kit\MANIFEST.txt" -Pattern '^image_digest: ').Line -split ' ')[1]
    $head = [IO.File]::ReadAllBytes($ParamFile)
    if ($head.Count -ge 2 -and (($head[0] -eq 0xFF -and $head[1] -eq 0xFE) -or ($head[0] -eq 0xFE -and $head[1] -eq 0xFF) -or ($head.Count -ge 3 -and $head[0] -eq 0xEF -and $head[1] -eq 0xBB -and $head[2] -eq 0xBF))) { throw 'STOP: the parameter file is UTF-16 or starts with a BOM, which oc refuses. Save it again as UTF-8 (Notepad: Save As, Encoding: UTF-8).' }
    if (-not (Select-String -LiteralPath $ParamFile -Pattern "^DEVKIT_DIGEST=$digest\s*$" -Quiet)) { throw "STOP: the parameter file must say DEVKIT_DIGEST=$digest (the kit MANIFEST image_digest)." }
    oc process --local -f "$Kit\bastion.template.yaml" --param-file $ParamFile | oc apply -f -
    if ($LASTEXITCODE -ne 0) { throw 'STOP: the bastion could not be rendered or applied. Fix the parameter named in the oc error above and run this step again.' }
    'Bastion applied'
}
```

## 4. Copy the kit into the bastion

`oc cp` takes relative paths here: it reads `D:` in an absolute Windows path as
a pod name.

<!-- devkit-runbook: step 4 -->
```powershell
. {
    $ErrorActionPreference = 'Stop'
    $name = Split-Path -Leaf $Kit
    if ($name -notmatch '^mfr-devkit-[0-9a-f]{64}$') { throw 'STOP: $Kit must name the kit directory itself, mfr-devkit-<key>, with no trailing backslash.' }
    oc wait --for=condition=Ready pod/mfr-bastion --timeout=600s
    if ($LASTEXITCODE -ne 0) { throw 'STOP: the bastion is not Ready. Run oc describe pod mfr-bastion and fix the event it shows (image pull, quota, admission).' }
    oc exec mfr-bastion -- sh -c "rm -rf /work/kits/$name && mkdir -p /work/kits /work/trust"
    if ($LASTEXITCODE -ne 0) { throw 'STOP: cannot prepare /work in the bastion. Check the mfr-bastion-work claim.' }
    Push-Location -LiteralPath (Split-Path -Parent $Kit)
    try {
        oc cp $name "mfr-bastion:/work/kits/$name"
        if ($LASTEXITCODE -ne 0) { throw 'STOP: oc cp of the kit failed. Run this step again.' }
    } finally { Pop-Location }
    Push-Location -LiteralPath (Split-Path -Parent $TrustedPub)
    try {
        oc cp (Split-Path -Leaf $TrustedPub) mfr-bastion:/work/trust/release-signing.pub
        if ($LASTEXITCODE -ne 0) { throw 'STOP: oc cp of the release key failed. Run this step again.' }
    } finally { Pop-Location }
    "Kit copied to /work/kits/$name"
}
```

## 5. Open the session and install

Your `oc` token and the robot credential are piped into the bastion's
memory-only session directory; neither is written to disk. `install.sh` then
checks the signature against the release key, every member, the inventory and
the MANIFEST, copies the image with skopeo, reads it back from the registry and
checks the bastion pod's `imageID`.

<!-- devkit-runbook: step 5 -->
```powershell
. {
    $ErrorActionPreference = 'Stop'
    $name = Split-Path -Leaf $Kit
    oc whoami -t | oc exec -i mfr-bastion -- mfr-session-login
    if ($LASTEXITCODE -ne 0) { throw 'STOP: session login failed. Run oc login again, then this step.' }
    oc get secret $Robot -o 'jsonpath={.data.\.dockerconfigjson}' | oc exec -i mfr-bastion -- mfr-session-login --registry
    if ($LASTEXITCODE -ne 0) { throw 'STOP: the registry credential was refused. Check $Robot, then run this step again.' }
    oc exec mfr-bastion -- env SNEAKERNET_TRUSTED_PUB=/work/trust/release-signing.pub sh "/work/kits/$name/install.sh" --registry $Registry --verify-pod mfr-bastion @InstallArgs
    if ($LASTEXITCODE -ne 0) { throw 'STOP: install.sh refused the kit; its FAIL line names the fix.' }
    'Dev kit installed. End the session with: oc exec mfr-bastion -- mfr-session-logout; oc logout'
}
```

## Fallback: Windows cannot push

Use this instead of step 2 when `oras.exe` cannot run or reach the registry
(endpoint policy, proxy, CA trust). It pushes from a pod on the previous
release's agent image, which is already in the site registry, with the kit's
static Linux `oras`. That image has no `tar`, so `oc cp` cannot write into it:
each file is streamed in through `cmd /c` input redirection, which keeps the
bytes unchanged, and `sha256sum -c` checks them inside the pod.

1. Run step 1 above.
2. Set `$PreviousAgentImage` to the previous release's agent image by digest
   (`<registry>/<path>@sha256:<hex>`, from that release's MANIFEST), then paste:

<!-- devkit-runbook: fallback -->
```powershell
. {
    $ErrorActionPreference = 'Stop'
    Set-Location -LiteralPath $Kit
    $key = ((Select-String -LiteralPath .\MANIFEST.txt -Pattern '^key: ').Line -split ' ')[1]
    $digest = ((Select-String -LiteralPath .\MANIFEST.txt -Pattern '^image_digest: ').Line -split ' ')[1]
    # A pod left by an earlier attempt is removed first, so the block can run again.
    oc delete pod mfr-devkit-push --ignore-not-found --wait=true
    if ($LASTEXITCODE -ne 0) { throw 'STOP: cannot remove an earlier mfr-devkit-push pod. Check your oc login.' }
    oc run mfr-devkit-push --image=$PreviousAgentImage --restart=Never --command -- sleep 7200
    if ($LASTEXITCODE -ne 0) { throw 'STOP: cannot start the push pod. Check $PreviousAgentImage and the namespace quota.' }
    try {
        oc wait --for=condition=Ready pod/mfr-devkit-push --timeout=600s
        if ($LASTEXITCODE -ne 0) { throw 'STOP: the push pod is not Ready. Run oc describe pod mfr-devkit-push, fix the event, then run this block again.' }
        oc exec mfr-devkit-push -- mkdir -p /tmp/kit/image/blobs/sha256
        $files = @('oras', 'SHA256SUMS') + (Get-ChildItem -LiteralPath .\image -Recurse -File | ForEach-Object { 'image/' + $_.FullName.Substring((Join-Path $Kit 'image').Length + 1).Replace('\', '/') })
        foreach ($rel in $files) {
            cmd /c "oc exec -i mfr-devkit-push -- dd of=/tmp/kit/$rel bs=1M status=none < $($rel.Replace('/', '\'))"
            if ($LASTEXITCODE -ne 0) { throw "STOP: streaming $rel into the pod failed. Run this block again." }
        }
        oc exec mfr-devkit-push -- sh -c 'cd /tmp/kit && sha256sum -c --ignore-missing --quiet SHA256SUMS && chmod 0755 oras'
        if ($LASTEXITCODE -ne 0) { throw 'STOP: a streamed file differs inside the pod. Run this block again.' }
        oc get secret $Robot -o 'jsonpath={.data.\.dockerconfigjson}' | oc exec -i mfr-devkit-push -- sh -c 'base64 -d > /tmp/auth.json'
        if ($LASTEXITCODE -ne 0) { throw 'STOP: cannot hand the robot credential to the pod. Check $Robot, then run this block again.' }
        oc exec mfr-devkit-push -- /tmp/kit/oras cp --from-oci-layout "/tmp/kit/image@$digest" "${Registry}:$key" --to-registry-config /tmp/auth.json
        if ($LASTEXITCODE -ne 0) { throw 'STOP: the push from the pod failed; its error names the cause. Run this block again once it is fixed.' }
        $readback = oc exec mfr-devkit-push -- /tmp/kit/oras resolve --registry-config /tmp/auth.json "${Registry}:$key"
        if ($readback -ne $digest) { throw "STOP: the registry reads back '$readback', not $digest. Do not create the bastion; report it." }
        "Pushed ${Registry}:$key = $digest from the fallback pod"
    } finally {
        # Every outcome deletes the pod, and with it the copied robot credential.
        oc delete pod mfr-devkit-push --ignore-not-found --wait=false
    }
}
```

3. Continue with step 3.

## Recorded site steps

CI proves the blocks above in Windows PowerShell 5.1 on a GitHub-hosted
`windows-2025` runner (`.github/workflows/devkit-proof.yml`), pushing to a
scratch registry with `oc` stood in. Record these at the site, once, in the
release record: endpoint policy on the unsigned `oras.exe`; HTTPS reach from
Windows to the registry and its CA trust; the push identity; `oc cp` of the
multi-GB kit; restricted-v2 admission of the bastion; the network probes; the
real quota; and, if used, the fallback pod's start and egress.
