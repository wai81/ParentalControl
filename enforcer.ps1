# Контролёр: запускается планировщиком от имени SYSTEM каждую минуту.
# Считает время ребёнка, предупреждает, завершает сеанс, закрывает запрещённые
# программы и блокирует сайты.

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'common.ps1')

try {
    $cfg = Get-PcConfig
    $user = "$($cfg.childUser)".Trim()
    if (-not $user) { exit 0 }

    $now = Get-Date
    $state = Get-PcState
    Reset-PcStateIfNewDay $state $now

    # 1. Сайты
    try {
        if (Update-PcHosts $cfg.blockedSites) { Write-PcLog "Список заблокированных сайтов обновлён" }
    } catch { Write-PcLog "Ошибка при обновлении hosts: $($_.Exception.Message)" }

    # Сколько минут прошло с прошлой проверки (не больше 2, чтобы не насчитать лишнего после сна/выключения)
    $elapsed = 1.0
    if ($state.lastTick) {
        try {
            $last = [datetime]::Parse($state.lastTick)
            $elapsed = [Math]::Max(0.0, [Math]::Min(2.0, ($now - $last).TotalMinutes))
        } catch { }
    }
    $state.lastTick = $now.ToString('o')

    $session = Get-PcChildSession $user
    if (-not $session) {
        $state.pendingLogoff = ''
        Save-PcState $state
        exit 0
    }
    $sid = [int]$session.SessionId

    # 2. Запрещённые программы
    $killed = Stop-PcBlockedApps $session.Processes $cfg.blockedApps
    if ($killed.Count -gt 0) {
        $names = ($killed | Select-Object -Unique) -join ', '
        Write-PcLog "Закрыты запрещённые программы: $names"
        Send-PcNotify "Эта программа запрещена родителями: $names"
    }

    # 3. Учёт времени (только когда ребёнок реально за компьютером)
    if (Test-PcChildActive $user $sid) { $state.usedMinutes += $elapsed }

    $day = Get-PcToday $cfg $state $now

    $reason = $null
    if ($state.blockedToday) {
        $reason = 'Родители заблокировали компьютер до конца дня.'
    } elseif (-not $day.InHours) {
        $reason = "Сейчас пользоваться компьютером нельзя. Разрешённое время: с $($day.From) до $($day.To)."
    } elseif ($day.LeftByLimit -le 0) {
        $reason = 'Время за компьютером на сегодня закончилось.'
    }

    if ($reason) {
        if ($state.pendingLogoff) {
            $pending = [datetime]::Parse($state.pendingLogoff)
            if (($now - $pending).TotalSeconds -ge 50) {
                Write-PcLog "Сеанс $user завершён. Причина: $reason"
                [void](Invoke-PcLogoff $sid)
                $state.pendingLogoff = ''
            }
        } else {
            $state.pendingLogoff = $now.ToString('o')
            Write-PcLog "Предупреждение о завершении сеанса: $reason"
            Send-PcNotify ($reason + "`r`n`r`nЧерез 1 минуту сеанс будет завершён. Сохрани свою работу!")
        }
    } else {
        $state.pendingLogoff = ''
        $rem = $day.Remaining
        $toWarn = @($cfg.warnAtMinutesLeft | Where-Object { $rem -le [double]$_ -and ($state.warned -notcontains [int]$_) } | ForEach-Object { [int]$_ })
        if ($toWarn.Count -gt 0) {
            $mins = [int][Math]::Max(1, [Math]::Ceiling($rem))
            Send-PcNotify "Осталось примерно $mins мин. работы за компьютером. Сохрани свою работу заранее."
            $state.warned = @($state.warned) + $toWarn
        }
    }

    Save-PcState $state
} catch {
    Write-PcLog "Ошибка контролёра: $($_.Exception.Message)"
    exit 1
}
