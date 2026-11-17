# Общие функции программы "Родительский контроль"
# Подключается из enforcer.ps1, parent.ps1 и install.ps1

# Исправляет ошибку PowerShell 5.1, из-за которой массивы иногда сохраняются в JSON неправильно
Remove-TypeData -TypeName System.Array -ErrorAction SilentlyContinue

$PcRoot      = Join-Path $env:ProgramData 'ParentalControl'
$ConfigPath  = Join-Path $PcRoot 'config.json'
$StatePath   = Join-Path $PcRoot 'state.json'
$HistoryPath = Join-Path $PcRoot 'history.csv'
$LogPath     = Join-Path $PcRoot 'log.txt'
$NotifyPath  = Join-Path $PcRoot 'notify.txt'
$PcTaskPath  = '\ParentalControl\'
$HostsPath   = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
$HostsBegin  = '# >>> ParentalControl BEGIN (managed automatically, do not edit)'
$HostsEnd    = '# <<< ParentalControl END'

# ---------- Журнал ----------
function Write-PcLog([string]$Message) {
    try {
        if ((Test-Path $LogPath) -and ((Get-Item $LogPath).Length -gt 1MB)) {
            Move-Item -Path $LogPath -Destination "$LogPath.old" -Force
        }
        $line = '{0:yyyy-MM-dd HH:mm:ss}  {1}' -f (Get-Date), $Message
        Add-Content -Path $LogPath -Value $line -Encoding UTF8
    } catch { }
}

# ---------- JSON ----------
function Read-JsonFile([string]$Path) {
    return (Get-Content -Path $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Write-JsonFile([string]$Path, $Object) {
    $tmp = "$Path.tmp"
    $Object | ConvertTo-Json -Depth 6 | Set-Content -Path $tmp -Encoding UTF8
    Move-Item -Path $tmp -Destination $Path -Force
}

# ---------- Настройки ----------
function New-PcDefaultConfig {
    return [pscustomobject]@{
        childUser         = ''
        limits            = [pscustomobject]@{ weekdays = 120; weekends = 180 }
        hours             = [pscustomobject]@{
            weekdays = [pscustomobject]@{ from = '08:00'; to = '21:00' }
            weekends = [pscustomobject]@{ from = '09:00'; to = '22:00' }
        }
        warnAtMinutesLeft = @(10, 5, 1)
        blockedApps       = @()
        blockedSites      = @()
    }
}

function Get-PcConfig {
    if (Test-Path $ConfigPath) { return (Read-JsonFile $ConfigPath) }
    return (New-PcDefaultConfig)
}

function Save-PcConfig($Config) { Write-JsonFile $ConfigPath $Config }

# ---------- Состояние за сегодня ----------
function Get-PcState {
    $state = [ordered]@{
        date          = ''
        usedMinutes   = 0.0
        bonusMinutes  = 0
        blockedToday  = $false
        lastTick      = ''
        pendingLogoff = ''
        warned        = @()
    }
    if (Test-Path $StatePath) {
        try {
            $j = Read-JsonFile $StatePath
            foreach ($k in @($state.Keys)) {
                if ($j.PSObject.Properties.Name -contains $k) { $state[$k] = $j.$k }
            }
        } catch { }
    }
    $state.usedMinutes  = [double]$state.usedMinutes
    $state.bonusMinutes = [int]$state.bonusMinutes
    $state.blockedToday = [bool]$state.blockedToday
    $state.warned       = @($state.warned | Where-Object { $_ -ne $null } | ForEach-Object { [int]$_ })
    return $state
}

function Save-PcState($State) { Write-JsonFile $StatePath $State }

# Сбрасывает счётчики, если наступил новый день (вчерашний итог пишется в историю)
function Reset-PcStateIfNewDay($State, [datetime]$Now, [switch]$NoHistory) {
    $today = $Now.ToString('yyyy-MM-dd')
    if ($State.date -ne $today) {
        if ($State.date -and -not $NoHistory) {
            try { Add-Content -Path $HistoryPath -Value ('{0};{1}' -f $State.date, [int][Math]::Round($State.usedMinutes)) -Encoding UTF8 } catch { }
        }
        $State.date          = $today
        $State.usedMinutes   = 0.0
        $State.bonusMinutes  = 0
        $State.blockedToday  = $false
        $State.lastTick      = ''
        $State.pendingLogoff = ''
        $State.warned        = @()
    }
}

# ---------- Время ----------
function ConvertTo-PcTime([string]$Text) {
    $t = "$Text".Trim()
    if ($t -eq '24:00') { return [TimeSpan]::FromHours(24) }
    if ($t -notmatch '^\d{1,2}:\d{2}$') { throw "Неверный формат времени: '$Text' (нужно ЧЧ:ММ)" }
    $ts = [TimeSpan]::Parse($t)
    if ($ts.TotalHours -ge 24) { throw "Неверное время: '$Text'" }
    return $ts
}

function Get-PcDayKind([datetime]$Date) {
    if ($Date.DayOfWeek -eq 'Saturday' -or $Date.DayOfWeek -eq 'Sunday') { return 'weekends' }
    return 'weekdays'
}

# Сводка на текущий момент: лимит, остаток, разрешённые часы
function Get-PcToday($Config, $State, [datetime]$Now) {
    $kind  = Get-PcDayKind $Now
    $from  = ConvertTo-PcTime $Config.hours.$kind.from
    $to    = ConvertTo-PcTime $Config.hours.$kind.to
    $t     = $Now.TimeOfDay
    $limit = [double]$Config.limits.$kind + [double]$State.bonusMinutes
    $inHours = ($t -ge $from) -and ($t -lt $to)
    $leftByLimit = $limit - $State.usedMinutes
    $leftByHours = ($to - $t).TotalMinutes
    $remaining = [Math]::Min($leftByLimit, $leftByHours)
    return [pscustomobject]@{
        Kind        = $kind
        From        = $Config.hours.$kind.from
        To          = $Config.hours.$kind.to
        InHours     = $inHours
        Limit       = $limit
        Used        = $State.usedMinutes
        LeftByLimit = $leftByLimit
        Remaining   = $remaining
    }
}

# ---------- Сеанс ребёнка ----------
function Test-PcUserMatch([string]$FullName, [string]$User) {
    if (-not $FullName) { return $false }
    return (($FullName -split '\\')[-1] -eq $User)
}

# Возвращает номер сеанса ребёнка и его процессы (или $null, если ребёнок не вошёл)
function Get-PcChildSession([string]$User) {
    $procs = @(Get-Process -IncludeUserName -ErrorAction SilentlyContinue |
        Where-Object { $_.SessionId -ne 0 -and (Test-PcUserMatch $_.UserName $User) })
    if ($procs.Count -eq 0) { return $null }
    return [pscustomobject]@{
        SessionId = $procs[0].SessionId
        Processes = $procs
    }
}

# Ребёнок сейчас за компьютером: его сеанс на экране и экран не заблокирован
function Test-PcChildActive([string]$User, [int]$SessionId) {
    $console = (Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction SilentlyContinue).UserName
    if (-not (Test-PcUserMatch $console $User)) { return $false }
    $locked = Get-Process -Name LogonUI -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq $SessionId }
    return (-not $locked)
}

function Invoke-PcLogoff([int]$SessionId) {
    if (-not ('PcWts' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class PcWts {
    [DllImport("wtsapi32.dll", SetLastError = true)]
    public static extern bool WTSLogoffSession(IntPtr hServer, int sessionId, bool bWait);
}
'@
    }
    return [PcWts]::WTSLogoffSession([IntPtr]::Zero, $SessionId, $false)
}

# Показывает ребёнку всплывающее сообщение (через задачу, работающую от его имени)
function Send-PcNotify([string]$Text) {
    try {
        Set-Content -Path $NotifyPath -Value $Text -Encoding UTF8
        Start-ScheduledTask -TaskPath $PcTaskPath -TaskName 'Notify' -ErrorAction Stop
    } catch {
        Write-PcLog "Не удалось показать уведомление: $($_.Exception.Message)"
    }
}

# ---------- Программы ----------
function Stop-PcBlockedApps($Processes, $Patterns) {
    $pats = @($Patterns | Where-Object { $_ } | ForEach-Object { ("$_".Trim()) -replace '\.exe$', '' })
    if ($pats.Count -eq 0) { return @() }
    $killed = @()
    foreach ($p in $Processes) {
        foreach ($pat in $pats) {
            if ($p.ProcessName -like $pat) {
                try { Stop-Process -Id $p.Id -Force -ErrorAction Stop; $killed += $p.ProcessName } catch { }
                break
            }
        }
    }
    return $killed
}

# ---------- Задачи планировщика ----------
function Register-PcTasks([string]$ChildUser) {
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

    # Контролёр: SYSTEM, каждую минуту. Ребёнок без прав администратора не может его остановить.
    $action = New-ScheduledTaskAction -Execute $ps -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $PcRoot 'enforcer.ps1'))
    try {
        $every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
    } catch {
        $every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
    }
    $boot = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'S-1-5-18' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
    Register-ScheduledTask -TaskPath $PcTaskPath -TaskName 'Enforcer' -Action $action -Trigger @($every, $boot) `
        -Principal $principal -Settings $settings -Description 'Parental control: time limits, apps and sites' -Force | Out-Null

    # Уведомления: запускаются от имени ребёнка, чтобы окно появилось на его экране
    $nAction = New-ScheduledTaskAction -Execute $ps -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $PcRoot 'notify.ps1'))
    $nPrincipal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\$ChildUser" -LogonType Interactive -RunLevel Limited
    $nSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances Parallel -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
    Register-ScheduledTask -TaskPath $PcTaskPath -TaskName 'Notify' -Action $nAction -Principal $nPrincipal `
        -Settings $nSettings -Description 'Parental control: messages to the child' -Force | Out-Null
}

function Unregister-PcTasks {
    foreach ($n in 'Enforcer', 'Notify') {
        Unregister-ScheduledTask -TaskPath $PcTaskPath -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue
    }
}

# ---------- Сайты (файл hosts) ----------
function ConvertTo-PcDomain([string]$Text) {
    $d = "$Text".Trim().ToLower()
    $d = $d -replace '^[a-z]+://', ''
    $d = ($d -split '[/?#:]')[0]
    $d = $d -replace '^www\.', ''
    if ($d -match '^[a-z0-9-]+(\.[a-z0-9-]+)+$') { return $d }
    return $null
}

function Get-PcHostsBlock($Sites) {
    $names = New-Object System.Collections.Generic.List[string]
    foreach ($s in @($Sites)) {
        $d = ConvertTo-PcDomain $s
        if (-not $d) { continue }
        foreach ($n in @($d, "www.$d", "m.$d")) {
            if (-not $names.Contains($n)) { $names.Add($n) }
        }
    }
    if ($names.Count -eq 0) { return '' }
    $lines = @($HostsBegin)
    foreach ($n in $names) { $lines += "0.0.0.0 $n"; $lines += ":: $n" }
    $lines += $HostsEnd
    return ($lines -join "`r`n")
}

# Возвращает $true, если файл hosts был изменён
function Update-PcHosts($Sites, [string]$Path = $HostsPath) {
    $content = ''
    if (Test-Path $Path) { $content = [IO.File]::ReadAllText($Path) }
    $pattern = '(?s)' + [regex]::Escape($HostsBegin) + '.*?' + [regex]::Escape($HostsEnd)
    $clean = ([regex]::Replace($content, $pattern, '')).TrimEnd()
    $block = Get-PcHostsBlock $Sites
    if ($block) { $new = $clean + "`r`n`r`n" + $block + "`r`n" } else { $new = $clean + "`r`n" }
    if ($new -ceq $content) { return $false }
    [IO.File]::WriteAllText($Path, $new, (New-Object System.Text.UTF8Encoding $false))
    try { & ipconfig.exe /flushdns | Out-Null } catch { }
    return $true
}
