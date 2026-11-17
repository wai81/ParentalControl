#Requires -RunAsAdministrator
# End-to-end check of the pcagent Windows service. Run from an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\test-service.ps1
# Installs the service from dist\pcagent, checks it, then removes it and its files.
# The agent has no rules.json during the test, so it restricts nothing.

$ErrorActionPreference = 'Stop'
$root      = Split-Path -Parent $PSScriptRoot
$buildExe  = Join-Path $root 'dist\pcagent\pcagent.exe'
$name      = 'ParentalControlAgent'
$installed = Join-Path $env:ProgramFiles 'ParentalControl\agent\pcagent.exe'
$data      = Join-Path $env:ProgramData 'ParentalControl\agent'
$logFile   = Join-Path $PSScriptRoot 'test-service.log'
$script:failed = 0
$script:began = $false

Start-Transcript -Path $logFile -Force | Out-Null

function Step([string]$Text) { Write-Host "`n=== $Text" -ForegroundColor Cyan }
function Check([bool]$Ok, [string]$Text) {
    if ($Ok) { Write-Host "OK   $Text" -ForegroundColor Green }
    else { Write-Host "FAIL $Text" -ForegroundColor Red; $script:failed++ }
}
function Get-Svc { Get-CimInstance Win32_Service -Filter "Name='$name'" }
function Wait-Running([int]$Seconds = 20) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        $s = Get-Svc
        if ($s -and $s.State -eq 'Running' -and $s.ProcessId) { return $s }
        Start-Sleep -Seconds 1
    }
    return (Get-Svc)
}

try {
    if (-not (Test-Path $buildExe)) { throw "Build not found: $buildExe (run scripts\build-agent.ps1)" }
    if (Test-Path $data) { throw "$data already exists. Remove it or the existing install first." }
    if (Get-Svc) { throw "Service $name is already installed. Remove it first." }
    $script:began = $true

    Step 'install'
    & $buildExe service install | Out-Host
    $svc = Wait-Running
    Check ($svc.State -eq 'Running') "service running (state: $($svc.State))"
    Check ($svc.StartName -eq 'LocalSystem') "runs as SYSTEM ($($svc.StartName))"
    Check ($svc.StartMode -eq 'Auto') "automatic start ($($svc.StartMode))"
    Check ($svc.PathName -like "*$installed*") "registered path: $($svc.PathName)"
    Check (Test-Path $installed) 'files copied to Program Files'
    sc.exe qfailure $name

    Step 'agent writes log and events'
    Start-Sleep -Seconds 8
    Check (Test-Path (Join-Path $data 'agent.log')) 'agent.log exists'
    Get-Content (Join-Path $data 'agent.log') -Encoding UTF8 -Tail 5
    $events = & $installed events -n 5
    $events
    Check (($events -join "`n") -match 'agent_started') 'agent_started event recorded'

    Step 'helper in the user session'
    # Rules for the current user with a whole-day window and a 24 h limit: nothing is restricted.
    $day = @{ limit_minutes = 1440; window = @{ start = '00:00'; end = '24:00' } }
    $rules = @{ child_user = $env:USERNAME; weekdays = $day; weekends = $day } | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText((Join-Path $data 'rules.json'), $rules, (New-Object Text.UTF8Encoding $false))
    function Get-Helper([int]$NotId = 0) {
        Get-Process pcagent -IncludeUserName -ErrorAction SilentlyContinue |
            Where-Object { $_.SessionId -ne 0 -and $_.Id -ne $NotId } | Select-Object -First 1
    }
    $helper = $null
    for ($i = 0; $i -lt 20 -and -not $helper; $i++) { Start-Sleep -Seconds 1; $helper = Get-Helper }
    Check ($null -ne $helper) "helper running in session $($helper.SessionId)"
    Check ($helper.UserName -like "*\$env:USERNAME") "helper runs as the user ($($helper.UserName))"
    Start-Sleep -Seconds 5
    $agentLog = Get-Content (Join-Path $data 'agent.log') -Encoding UTF8 -Raw
    Check ($agentLog -match 'Helper подключился') 'helper connected to the service pipe'
    & $installed status | Select-Object -Last 4

    Step 'helper restarted after it is killed'
    Stop-Process -Id $helper.Id -Force
    $helper2 = $null
    for ($i = 0; $i -lt 25 -and -not $helper2; $i++) { Start-Sleep -Seconds 1; $helper2 = Get-Helper $helper.Id }
    Check ($null -ne $helper2) "new helper started ($($helper.Id) -> $($helper2.Id))"
    $events = & $installed events -n 30
    Check (($events -join "`n") -match 'helper_stopped') 'helper_stopped event recorded'

    Step 'data folder permissions'
    $acl = icacls $data
    $acl
    $usersLine = $acl | Where-Object { $_ -match 'BUILTIN\\(Users|Пользователи)' }
    Check ($usersLine -and $usersLine -notmatch '\(F\)|\(M\)|\(W') 'Users have read-only access'
    Check (-not ($acl -match 'CREATOR OWNER|СОЗДАТЕЛЬ-ВЛАДЕЛЕЦ')) 'no inherited CREATOR OWNER entry'

    Step 'restart after the process is killed'
    $pid1 = $svc.ProcessId
    Stop-Process -Id $pid1 -Force
    Start-Sleep -Seconds 3
    $svc = Wait-Running 20
    Check ($svc.State -eq 'Running' -and $svc.ProcessId -ne $pid1) "restarted (pid $pid1 -> $($svc.ProcessId))"

    Step 'stop / start via CLI'
    & $installed service stop | Out-Host
    Check ((Get-Svc).State -eq 'Stopped') 'stopped'
    & $installed service start | Out-Host
    Check ((Wait-Running).State -eq 'Running') 'started again'
    Check ((Get-Content (Join-Path $data 'agent.log') -Encoding UTF8 -Raw) -match 'остановлен') 'clean stop logged'

    Step 'upgrade (install over existing)'
    & $buildExe service install | Out-Host
    Check ((Wait-Running).State -eq 'Running') 'running after upgrade'

    Step 'remove'
    & $buildExe service remove | Out-Host
    Check (-not (Get-Svc)) 'service removed'
}
catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    $script:failed++
}
finally {
    Step 'cleanup'
    if (-not $script:began) { Write-Host 'nothing to clean up' }
    elseif (Get-Svc) { & $buildExe service remove | Out-Host }
    # The helper exits by itself ~15 s after the service is gone; do not wait for it.
    if ($script:began) { Get-Process pcagent -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep -Seconds 1 }
    if ($script:began) { foreach ($dir in @((Split-Path -Parent $installed), $data)) {
        if (Test-Path $dir) { Remove-Item $dir -Recurse -Force; Write-Host "removed $dir" }
    } }
    foreach ($parent in @((Join-Path $env:ProgramFiles 'ParentalControl'))) {
        if ((Test-Path $parent) -and -not (Get-ChildItem $parent)) { Remove-Item $parent }
    }
    Write-Host "`nFAILED CHECKS: $script:failed"
    Stop-Transcript | Out-Null
    Write-Host "Log: $logFile"
}
