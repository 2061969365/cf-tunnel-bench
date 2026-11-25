<#
.SYNOPSIS
  Stop what start-bench.ps1 started (origin server + this tool's cloudflared).

.DESCRIPTION
  Only processes recorded in .bench\*.pid are touched, and only if their image
  name still matches python / cloudflared. The production tunnel is never
  affected.
#>
[CmdletBinding()]
param([switch]$Quiet)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bench = Join-Path $root '.bench'

if (-not (Test-Path -LiteralPath $bench)) {
    if (-not $Quiet) { Write-Host 'nothing to stop (.bench not found)' }
    return
}

function Stop-PidFile([string]$Name, [string[]]$Expected) {
    $pidFile = Join-Path $bench "$Name.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) { return }
    $raw = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    $procId = 0
    if (-not [int]::TryParse($raw, [ref]$procId)) {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        return
    }
    $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($p -and ($Expected -contains $p.ProcessName)) {
        try {
            Stop-Process -Id $procId -Force
            if (-not $Quiet) { Write-Host "stopped $Name (pid $procId, $($p.ProcessName))" }
        } catch {
            if (-not $Quiet) { Write-Warning "could not stop $Name (pid $procId): $_" }
        }
    } elseif ($p) {
        if (-not $Quiet) { Write-Warning "pid $procId is $($p.ProcessName), not $($Expected -join '/'); leaving it alone" }
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

Stop-PidFile 'tunnel' @('cloudflared')
Stop-PidFile 'origin' @('python', 'py', 'pythonw')

if (-not $Quiet) { Write-Host 'done' }
