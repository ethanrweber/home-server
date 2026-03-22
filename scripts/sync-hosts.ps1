#Requires -RunAsAdministrator
# Syncs the Windows hosts file with domains from the Caddyfile.
# Reads LOCAL_STATIC_IP from .env and parses domains from the Caddyfile,
# then updates a managed section in the hosts file.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$envFile = Join-Path $repoRoot ".env"
$hostsFile = "C:\Windows\System32\drivers\etc\hosts"

# Read LOCAL_STATIC_IP from .env
$ip = Get-Content $envFile |
    Where-Object { $_ -match "^\s*LOCAL_STATIC_IP\s*=" } |
    ForEach-Object { ($_ -split "=", 2)[1].Trim() }

if (-not $ip) {
    Write-Error "LOCAL_STATIC_IP not found in $envFile"
    exit 1
}

# Read Caddyfile path from .env
$configRoot = Get-Content $envFile |
    Where-Object { $_ -match "^\s*CONFIG_ROOT\s*=" } |
    ForEach-Object { ($_ -split "=", 2)[1].Trim() }

$caddyfile = Join-Path $configRoot "Caddy\Caddyfile"

if (-not (Test-Path $caddyfile)) {
    Write-Error "Caddyfile not found at $caddyfile"
    exit 1
}

# Parse domains from Caddyfile (matches "http://domain {" or "https://domain {" or "domain {")
$domains = Get-Content $caddyfile |
    Select-String -Pattern "^\s*(?:https?://)?([^\s{:]+)\s*\{" |
    ForEach-Object { $_.Matches.Groups[1].Value }

# Build managed section
$lines = @("# CaddyHostsSectionStart", "# Managed by scripts/sync-hosts.ps1 - do not edit manually.")
foreach ($d in $domains) {
    $lines += "$ip  $d"
}
$lines += "# CaddyHostsSectionEnd"
$newSection = $lines -join "`r`n"

# Update hosts file
$hosts = [System.IO.File]::ReadAllText($hostsFile)

if ($hosts -match "(?s)# CaddyHostsSectionStart.*?# CaddyHostsSectionEnd") {
    $hosts = [regex]::Replace($hosts, "(?s)# CaddyHostsSectionStart.*?# CaddyHostsSectionEnd", $newSection)
} else {
    $hosts = $hosts.TrimEnd() + "`r`n`r`n" + $newSection + "`r`n"
}

[System.IO.File]::WriteAllText($hostsFile, $hosts)
Write-Host "Updated hosts file with $($domains.Count) Caddy domain(s):"
foreach ($d in $domains) {
    Write-Host "  $ip  $d"
}
