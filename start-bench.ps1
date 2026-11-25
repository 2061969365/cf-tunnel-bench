<#
.SYNOPSIS
  Start the cf-tunnel-bench origin server and a dedicated cloudflared connector.

.DESCRIPTION
  Never touches the production tunnel in ~/.cloudflared/config.yml. Everything
  this script starts is recorded under .bench\ and can be stopped with
  stop-bench.ps1.

.EXAMPLE
  .\start-bench.ps1
  .\start-bench.ps1 -Mode Named -Hostname bench.fastfaster.kdns.fr
#>
[CmdletBinding()]
param(
    [ValidateSet('Quick', 'Named')]
    [string]$Mode = 'Quick',
    [int]$Port = 18080,
    [string]$Hostname = '',
    [string]$TunnelName = 'cf-tunnel-bench',
    [string]$Cloudflared = '',
    [ValidateSet('http2', 'quic', 'auto')]
    [string]$Protocol = 'http2'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bench = Join-Path $root '.bench'
New-Item -ItemType Directory -Force -Path $bench | Out-Null

function Resolve-Python {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw 'python not found in PATH'
}

function Resolve-Cloudflared([string]$Explicit) {
    if ($Explicit) {
        if (-not (Test-Path -LiteralPath $Explicit)) { throw "cloudflared not found: $Explicit" }
        return (Resolve-Path -LiteralPath $Explicit).Path
    }
    $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        (Join-Path $root 'bin\cloudflared.exe'),
        (Join-Path $env:ProgramFiles 'cloudflared\cloudflared.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'cloudflared\cloudflared.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\cloudflared.exe'),
        (Join-Path $env:ProgramData 'chocolatey\bin\cloudflared.exe')
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return (Resolve-Path -LiteralPath $c).Path }
    }
    throw 'cloudflared not found. Install it (winget install --id Cloudflare.cloudflared) or pass -Cloudflared <path>.'
}

function Wait-Healthy([string]$Url, [int]$Attempts = 40) {
    for ($i = 0; $i -lt $Attempts; $i++) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
            if ($r.StatusCode -eq 200) { return $true }
        } catch {
            Start-Sleep -Milliseconds 300
        }
    }
    return $false
}

$python = Resolve-Python
$cf = Resolve-Cloudflared $Cloudflared

$token = -join (1..16 | ForEach-Object { '{0:x}' -f (Get-Random -Minimum 0 -Maximum 16) })
Set-Content -LiteralPath (Join-Path $bench 'token.txt') -Value $token -NoNewline
$originOut = Join-Path $bench 'origin.out.log'
$originErr = Join-Path $bench 'origin.err.log'

$origin = Start-Process -FilePath $python `
    -ArgumentList @('-u', (Join-Path $root 'bench_origin.py'), '--port', "$Port", '--token', $token) `
    -WorkingDirectory $root `
    -RedirectStandardOutput $originOut `
    -RedirectStandardError $originErr `
    -WindowStyle Hidden -PassThru
Set-Content -Path (Join-Path $bench 'origin.pid') -Value $origin.Id

$health = "http://127.0.0.1:$Port/$token/healthz"
if (-not (Wait-Healthy $health)) {
    try { Stop-Process -Id $origin.Id -Force -ErrorAction SilentlyContinue } catch { }
    throw "origin did not become healthy on port $Port; see $originErr"
}

$tunnelErr = Join-Path $bench 'tunnel.err.log'
$tunnelOut = Join-Path $bench 'tunnel.out.log'
Remove-Item -LiteralPath $tunnelErr, $tunnelOut -Force -ErrorAction SilentlyContinue

$publicUrl = $null

if ($Mode -eq 'Quick') {
    # A bench-local config keeps cloudflared from reading ~/.cloudflared/config.yml,
    # which would drag in the production tunnel and its credentials.
    $quickCfg = Join-Path $bench 'quick-config.yml'
    $quickCfgText = @"
protocol: $Protocol
edge-ip-version: "4"
no-autoupdate: true
"@
    Set-Content -LiteralPath $quickCfg -Value $quickCfgText -Encoding ASCII

    $tunnel = Start-Process -FilePath $cf `
        -ArgumentList @('tunnel', '--config', $quickCfg, '--url', "http://127.0.0.1:$Port") `
        -WorkingDirectory $root `
        -RedirectStandardOutput $tunnelOut `
        -RedirectStandardError $tunnelErr `
        -WindowStyle Hidden -PassThru
    Set-Content -Path (Join-Path $bench 'tunnel.pid') -Value $tunnel.Id

    for ($i = 0; $i -lt 80; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Path -LiteralPath $tunnelErr) {
            $m = Select-String -LiteralPath $tunnelErr -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -AllMatches |
                Select-Object -Last 1
            if ($m) { $publicUrl = $m.Matches[0].Value; break }
        }
        if ($tunnel.HasExited) {
            $tail = Get-Content -LiteralPath $tunnelErr -Raw -ErrorAction SilentlyContinue
            throw "cloudflared exited before the URL was ready:`n$tail"
        }
    }
    if (-not $publicUrl) { throw "timed out waiting for the quick-tunnel URL; see $tunnelErr" }
} else {
    if (-not $Hostname) { throw '-Hostname is required with -Mode Named' }
    $cert = Join-Path $env:USERPROFILE '.cloudflared\cert.pem'
    if (-not (Test-Path -LiteralPath $cert)) { throw "cert.pem not found at $cert" }

    $id = $null
    try {
        $out = & $cf tunnel create $TunnelName 2>&1 | Out-String
        $m = [regex]::Match($out, 'with id ([0-9a-f]{8}-[0-9a-f-]{27})')
        if ($m.Success) { $id = $m.Groups[1].Value }
    } catch { }
    if (-not $id) {
        $json = & $cf tunnel list --output json 2>$null | Out-String
        if ($json) {
            $list = $json | ConvertFrom-Json
            $hit = $list | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
            if ($hit) { $id = $hit.id }
        }
    }
    if (-not $id) { throw "could not resolve tunnel id for '$TunnelName'" }

    & $cf tunnel route dns $TunnelName $Hostname 2>&1 | ForEach-Object { Write-Host $_ }

    $credFile = Join-Path $env:USERPROFILE ".cloudflared\$id.json"
    if (-not (Test-Path -LiteralPath $credFile)) { throw "expected credentials at $credFile" }

    $cfgPath = Join-Path $bench 'named-config.yml'
    $cfg = @"
tunnel: $id
credentials-file: $($credFile -replace '\\','\\')
protocol: $Protocol
edge-ip-version: "4"
ingress:
  - hostname: $Hostname
    service: http://127.0.0.1:$Port
  - service: http_status:404
"@
    Set-Content -LiteralPath $cfgPath -Value $cfg -Encoding ASCII

    $tunnel = Start-Process -FilePath $cf `
        -ArgumentList @('tunnel', '--config', $cfgPath, 'run', $TunnelName) `
        -WorkingDirectory $root `
        -RedirectStandardOutput $tunnelOut `
        -RedirectStandardError $tunnelErr `
        -WindowStyle Hidden -PassThru
    Set-Content -Path (Join-Path $bench 'tunnel.pid') -Value $tunnel.Id
    Start-Sleep -Seconds 4
    $publicUrl = "https://$Hostname"
}

$full = "$publicUrl/$token/stream"
Set-Content -LiteralPath (Join-Path $bench 'last-url.txt') -Value $full -NoNewline

Write-Host ''
Write-Host ' cf-tunnel-bench is up' -ForegroundColor Green
Write-Host "  origin  : http://127.0.0.1:$Port  (token $token)"
Write-Host "  public  : $publicUrl"
Write-Host "  bench   : $full"
Write-Host "  mode    : $Mode, protocol $Protocol"
Write-Host ''
Write-Host "  local   : python bench.py --url `"http://127.0.0.1:$Port/$token/stream`" --seconds 10 --streams 1,4 --reps 1"
Write-Host "  stop    : .\stop-bench.ps1"
