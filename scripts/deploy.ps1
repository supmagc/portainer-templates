<#
.SYNOPSIS
  Deploys a file (or every file under a directory) from .\hosts\<Host>\<Path> to
  <Host>:/<Path> via scp.

.DESCRIPTION
  The tree under hosts\<Host>\ mirrors that host's real destination filesystem
  exactly, so "strip the hosts\<Host>\ prefix, add a leading /" is the entire
  mapping from local path to remote path - no per-file config needed.

  Only commit files under hosts\ that are safe to have in git history - no
  tokens, passwords, or other secrets, and nothing under manual/UI control
  elsewhere (e.g. Grafana's contactpoints.yml/policies.yml stay host-only,
  see scratchpad\host-configs\ instead).

  Hosts are discovered from ~\.ssh\config (Host entries, wildcards excluded) -
  add one there before it'll show up here, e.g.:
    Host nas
      HostName 192.168.1.x
      User root

  Both prompts remember your last choice as the default (blank input at the
  prompt accepts it) - per-host for the file prompt, since different hosts
  have different files.

.PARAMETER DeployHost
  Skip the host prompt and deploy to this host directly.

.PARAMETER DeployPath
  Skip the file prompt and deploy this path directly (relative to
  hosts\<DeployHost>\, forward or back slashes both fine). A directory
  deploys every file under it. Requires -DeployHost.

.EXAMPLE
  .\scripts\deploy.ps1
  Fully interactive: pick a host, then a file, from discovered lists.

.EXAMPLE
  .\scripts\deploy.ps1 -DeployHost nas -DeployPath mnt/fastpool/system/processes/grafana/provisioning/dashboards/json/network-details.json
  No prompts - deploys exactly that one file.
#>
param(
    [string]$DeployHost,
    [string]$DeployPath
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$HostsDir = Resolve-Path (Join-Path $ScriptDir '..\hosts')
$StateFile = Join-Path $ScriptDir '..\.deploy-state.json'

function Get-SshConfigHosts {
    $sshConfig = Join-Path $HOME '.ssh\config'
    if (-not (Test-Path $sshConfig)) { return @() }
    $names = New-Object System.Collections.Generic.List[string]
    foreach ($line in Get-Content $sshConfig) {
        if ($line -match '^\s*Host\s+(.+)$') {
            foreach ($token in ($Matches[1] -split '\s+')) {
                if ($token -and $token -notmatch '[*?]') { $names.Add($token) }
            }
        }
    }
    $names | Select-Object -Unique
}

function Get-DeployState {
    if (Test-Path $StateFile) {
        try { return Get-Content $StateFile -Raw | ConvertFrom-Json } catch { }
    }
    return [pscustomobject]@{}
}

function Set-StateProperty($state, $name, $value) {
    if ($state.PSObject.Properties[$name]) {
        $state.$name = $value
    } else {
        $state | Add-Member -NotePropertyName $name -NotePropertyValue $value
    }
    return $state
}

$state = Get-DeployState

# --- pick host ---
if (-not $DeployHost) {
    $sshHosts = @(Get-SshConfigHosts)
    if (-not $sshHosts) {
        Write-Error "No hosts found in $HOME\.ssh\config - add a Host entry first."
    }
    $lastHost = $state.lastHost
    Write-Host "Select a host:"
    for ($i = 0; $i -lt $sshHosts.Count; $i++) {
        $marker = if ($sshHosts[$i] -eq $lastHost) { '  (default)' } else { '' }
        Write-Host "  [$($i + 1)] $($sshHosts[$i])$marker"
    }
    $selection = Read-Host "Enter number (blank = default)"
    if ([string]::IsNullOrWhiteSpace($selection)) {
        if (-not $lastHost) { Write-Error "No default host set yet - pick one." }
        $DeployHost = $lastHost
    } else {
        $DeployHost = $sshHosts[[int]$selection - 1]
    }
}

$hostDir = Join-Path $HostsDir $DeployHost
if (-not (Test-Path $hostDir)) {
    Write-Error "No local files configured for host '$DeployHost' under $hostDir"
}

# Accept a full absolute path too (e.g. pasted via "Copy Path") by stripping the
# hostDir prefix - Join-Path mishandles combining two absolute paths otherwise
# (silently drops the drive letter rather than erroring).
if ($DeployPath) {
    $DeployPath = $DeployPath.Trim('"', "'")
    if ([System.IO.Path]::IsPathRooted($DeployPath)) {
        $normalized = $DeployPath.Replace('/', '\')
        $prefix = $hostDir.TrimEnd('\') + '\'
        if ($normalized.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            $DeployPath = $normalized.Substring($prefix.Length) -replace '\\', '/'
        } else {
            Write-Error "DeployPath '$DeployPath' is an absolute path outside $hostDir - pass a path relative to that folder instead."
        }
    }
}

# --- pick file ---
if (-not $DeployPath) {
    $files = @(
        Get-ChildItem -Path $hostDir -Recurse -File |
            ForEach-Object { $_.FullName.Substring($hostDir.Length + 1) -replace '\\', '/' } |
            Sort-Object
    )
    $lastPathProp = "lastPath_$DeployHost"
    $lastPath = $state.$lastPathProp

    Write-Host "Select a file to deploy to ${DeployHost}:"
    for ($i = 0; $i -lt $files.Count; $i++) {
        $marker = if ($files[$i] -eq $lastPath) { '  (default)' } else { '' }
        Write-Host "  [$($i + 1)] $($files[$i])$marker"
    }
    Write-Host "  [A] All files for $DeployHost"
    $selection = Read-Host "Enter number/A (blank = default)"
    if ([string]::IsNullOrWhiteSpace($selection)) {
        if (-not $lastPath) { Write-Error "No default file set yet for '$DeployHost' - pick one." }
        $DeployPath = $lastPath
    } elseif ($selection -match '^[Aa]$') {
        $DeployPath = ''
    } else {
        $DeployPath = $files[[int]$selection - 1]
    }
}

# --- resolve local files to send ---
$localFiles = @()
if ([string]::IsNullOrEmpty($DeployPath)) {
    $localFiles = @(Get-ChildItem -Path $hostDir -Recurse -File | Select-Object -ExpandProperty FullName)
} else {
    $fullPath = Join-Path $hostDir ($DeployPath -replace '/', '\')
    if (Test-Path $fullPath -PathType Container) {
        $localFiles = @(Get-ChildItem -Path $fullPath -Recurse -File | Select-Object -ExpandProperty FullName)
    } elseif (Test-Path $fullPath -PathType Leaf) {
        $localFiles = @($fullPath)
    } else {
        Write-Error "No such file or directory: $fullPath"
    }
}

if (-not $localFiles) {
    Write-Error "Nothing to deploy."
}

# Deploy writes to the host's real filesystem paths, which needs root. Hosts we
# connect to as root (e.g. nas) get a direct scp. Hosts we connect to as an
# unprivileged user (e.g. openhabian) can't scp into /etc or /usr, so instead we
# stage the files under ~/.oh-deploy-stage/<path> and let a small root helper
# (/usr/local/sbin/oh-deploy-apply, itself tracked under hosts/<host>/) move them
# into place - one sudo password prompt per run.
$remoteUser = (
    ssh -G $DeployHost 2>$null |
        Where-Object { $_ -match '^user\s+(.+)$' } |
        ForEach-Object { $Matches[1].Trim() } |
        Select-Object -First 1
)
$useStaging = $remoteUser -and $remoteUser -ne 'root'

if ($useStaging) {
    $stageDir = '.oh-deploy-stage'
    Write-Host "Host '$DeployHost' connects as '$remoteUser' - staging then applying with sudo."
    ssh $DeployHost "rm -rf '$stageDir' && mkdir -p '$stageDir'"
    foreach ($localFile in $localFiles) {
        $relative = $localFile.Substring($hostDir.Length + 1) -replace '\\', '/'
        $stagePath = "$stageDir/$relative"
        $stageParent = $stagePath -replace '/[^/]*$', ''
        Write-Host "==> $localFile -> ${DeployHost}:/$relative  (staged)"
        ssh $DeployHost "mkdir -p '$stageParent'"
        scp $localFile "${DeployHost}:$stagePath"
    }
    Write-Host ""
    Write-Host "Applying as root on $DeployHost (enter your sudo password if prompted):"
    ssh -t $DeployHost "sudo /usr/local/sbin/oh-deploy-apply"
} else {
    foreach ($localFile in $localFiles) {
        $relative = $localFile.Substring($hostDir.Length + 1) -replace '\\', '/'
        $remotePath = "/$relative"
        $remoteDir = $remotePath -replace '/[^/]*$', ''
        Write-Host "==> $localFile -> ${DeployHost}:$remotePath"
        ssh $DeployHost "mkdir -p '$remoteDir'"
        scp $localFile "${DeployHost}:$remotePath"
    }
}

# --- remember selections ---
$state = Set-StateProperty $state 'lastHost' $DeployHost
$state = Set-StateProperty $state "lastPath_$DeployHost" $DeployPath
$state | ConvertTo-Json | Set-Content -Path $StateFile -Encoding utf8

Write-Host ""
Write-Host "Done. This only copies files - restart/reload the relevant service on $DeployHost yourself."
