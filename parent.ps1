#Requires -RunAsAdministrator
# Панель родителя: просмотр статистики и изменение настроек

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'common.ps1')

function Read-Int([string]$Prompt, [int]$Min, [int]$Max) {
    while ($true) {
        $v = Read-Host $Prompt
        if ([string]::IsNullOrWhiteSpace($v)) { return $null }
        $n = 0
        if ([int]::TryParse($v.Trim(), [ref]$n) -and $n -ge $Min -and $n -le $Max) { return $n }
        Write-Host "  Введите число от $Min до $Max (или Enter для отмены)." -ForegroundColor Yellow
    }
}

function Read-YesNo([string]$Prompt) {
    return ((Read-Host "$Prompt (д/н)") -match '^(д|y|да|yes)$')
}

function Format-Min([double]$m) {
    $m = [Math]::Max(0, [Math]::Round($m))
    $h = [Math]::Floor($m / 60); $r = $m % 60
    if ($h -gt 0) { return ('{0} ч {1} мин' -f $h, $r) }
    return ('{0} мин' -f $r)
}

function Get-FreshState {
    $s = Get-PcState
    Reset-PcStateIfNewDay $s (Get-Date) -NoHistory
    return $s
}

function Show-Status {
    $cfg = Get-PcConfig
    $state = Get-FreshState
    $day = Get-PcToday $cfg $state (Get-Date)
    $session = Get-PcChildSession $cfg.childUser

    Write-Host ''
    Write-Host "Ребёнок: $($cfg.childUser)" -ForegroundColor Cyan
    if ($session) { Write-Host '  Сейчас в системе: да' } else { Write-Host '  Сейчас в системе: нет' }
    Write-Host ("  Сегодня использовано: {0} из {1}" -f (Format-Min $day.Used), (Format-Min $day.Limit))
    if ($state.bonusMinutes -ne 0) { Write-Host "  (включая бонус: $($state.bonusMinutes) мин)" }
    Write-Host ("  Осталось: {0}" -f (Format-Min $day.LeftByLimit))
    Write-Host "  Разрешённые часы сегодня: $($day.From) – $($day.To)"
    if ($state.blockedToday) { Write-Host '  Компьютер заблокирован до конца дня.' -ForegroundColor Yellow }
    Write-Host ''
    Write-Host "  Будни:    $($cfg.limits.weekdays) мин, $($cfg.hours.weekdays.from) – $($cfg.hours.weekdays.to)"
    Write-Host "  Выходные: $($cfg.limits.weekends) мин, $($cfg.hours.weekends.from) – $($cfg.hours.weekends.to)"
    $sites = @($cfg.blockedSites); $apps = @($cfg.blockedApps)
    if ($sites.Count) { Write-Host "  Сайты:     $($sites -join ', ')" } else { Write-Host '  Сайты:     (нет)' }
    if ($apps.Count)  { Write-Host "  Программы: $($apps -join ', ')" }  else { Write-Host '  Программы: (нет)' }

    if (Test-Path $HistoryPath) {
        $hist = @(Get-Content $HistoryPath -Encoding UTF8 | Where-Object { $_ } | Select-Object -Last 7)
        if ($hist.Count) {
            Write-Host ''
            Write-Host '  Последние дни:'
            foreach ($line in $hist) {
                $p = $line -split ';'
                Write-Host ('    {0}: {1}' -f $p[0], (Format-Min ([double]$p[1])))
            }
        }
    }

    $task = Get-ScheduledTask -TaskPath $PcTaskPath -TaskName 'Enforcer' -ErrorAction SilentlyContinue
    Write-Host ''
    if ($task -and $task.State -ne 'Disabled') {
        Write-Host '  Контроль работает.' -ForegroundColor Green
    } else {
        Write-Host '  Контроль НЕ работает! Выберите пункт «Восстановить» или переустановите программу.' -ForegroundColor Red
    }
}

function Add-Bonus {
    $n = Read-Int 'Сколько минут добавить на сегодня (можно отрицательное, чтобы убавить)' -600 600
    if ($null -eq $n) { return }
    $state = Get-FreshState
    $state.bonusMinutes += $n
    $state.warned = @()
    $state.pendingLogoff = ''
    Save-PcState $state
    Write-PcLog "Родитель изменил время на сегодня: $n мин"
    Write-Host "Готово. Бонус на сегодня: $($state.bonusMinutes) мин." -ForegroundColor Green
}

function Set-BlockToday {
    $state = Get-FreshState
    if ($state.blockedToday) {
        if (Read-YesNo 'Компьютер заблокирован до конца дня. Снять блокировку?') {
            $state.blockedToday = $false; $state.pendingLogoff = ''
            Save-PcState $state
            Write-PcLog 'Родитель снял блокировку на сегодня'
            Write-Host 'Блокировка снята.' -ForegroundColor Green
        }
    } elseif (Read-YesNo 'Заблокировать компьютер для ребёнка до конца дня? (сеанс завершится примерно через минуту)') {
        $state.blockedToday = $true
        Save-PcState $state
        Write-PcLog 'Родитель заблокировал компьютер до конца дня'
        Start-ScheduledTask -TaskPath $PcTaskPath -TaskName 'Enforcer' -ErrorAction SilentlyContinue
        Write-Host 'Готово. Ребёнок получит предупреждение, затем сеанс завершится.' -ForegroundColor Green
    }
}

function Set-Limits {
    $cfg = Get-PcConfig
    Write-Host 'Нажмите Enter, чтобы оставить текущее значение.'
    $w = Read-Int "  Будни, минут в день (сейчас $($cfg.limits.weekdays))" 0 1440
    if ($null -ne $w) { $cfg.limits.weekdays = $w }
    $e = Read-Int "  Выходные, минут в день (сейчас $($cfg.limits.weekends))" 0 1440
    if ($null -ne $e) { $cfg.limits.weekends = $e }
    Save-PcConfig $cfg
    Write-PcLog "Лимиты изменены: будни $($cfg.limits.weekdays), выходные $($cfg.limits.weekends)"
    Write-Host 'Сохранено.' -ForegroundColor Green
}

function Read-TimeOrKeep([string]$Prompt, [string]$Current) {
    while ($true) {
        $v = Read-Host "$Prompt (сейчас $Current)"
        if ([string]::IsNullOrWhiteSpace($v)) { return $Current }
        try { [void](ConvertTo-PcTime $v); return $v.Trim() } catch { Write-Host "  $($_.Exception.Message)" -ForegroundColor Yellow }
    }
}

function Set-Hours {
    $cfg = Get-PcConfig
    Write-Host 'Формат ЧЧ:ММ, например 08:00 или 21:30 (можно 24:00). Enter — оставить как есть.'
    foreach ($k in 'weekdays', 'weekends') {
        $title = if ($k -eq 'weekdays') { 'Будни' } else { 'Выходные' }
        while ($true) {
            $f = Read-TimeOrKeep "  $title — с" $cfg.hours.$k.from
            $t = Read-TimeOrKeep "  $title — до" $cfg.hours.$k.to
            if ((ConvertTo-PcTime $f) -lt (ConvertTo-PcTime $t)) { break }
            Write-Host '  Время "с" должно быть раньше времени "до".' -ForegroundColor Yellow
        }
        $cfg.hours.$k.from = $f; $cfg.hours.$k.to = $t
    }
    Save-PcConfig $cfg
    Write-PcLog 'Изменены разрешённые часы'
    Write-Host 'Сохранено.' -ForegroundColor Green
}

function Edit-List([string]$Field, [string]$Title, [string]$Example) {
    while ($true) {
        $cfg = Get-PcConfig
        $items = @($cfg.$Field | Where-Object { $_ })
        Write-Host ''
        Write-Host "${Title}:" -ForegroundColor Cyan
        if ($items.Count -eq 0) { Write-Host '  (список пуст)' }
        for ($i = 0; $i -lt $items.Count; $i++) { Write-Host ("  {0}. {1}" -f ($i + 1), $items[$i]) }
        Write-Host ''
        Write-Host "  Введите новое значение, чтобы добавить (например: $Example)"
        Write-Host '  Введите -номер, чтобы удалить (например: -1)'
        $v = Read-Host '  Enter — назад'
        if ([string]::IsNullOrWhiteSpace($v)) { return }
        $v = $v.Trim()
        if ($v -match '^-(\d+)$') {
            $idx = [int]$Matches[1] - 1
            if ($idx -ge 0 -and $idx -lt $items.Count) {
                $removed = $items[$idx]
                $cfg.$Field = @($items | Where-Object { $_ -ne $removed })
                Save-PcConfig $cfg
                Write-PcLog "$Title — удалено: $removed"
            }
        } else {
            if ($Field -eq 'blockedSites') {
                $d = ConvertTo-PcDomain $v
                if (-not $d) { Write-Host '  Не похоже на адрес сайта.' -ForegroundColor Yellow; continue }
                $v = $d
            } else {
                $v = $v -replace '\.exe$', ''
            }
            if ($items -notcontains $v) {
                $cfg.$Field = @($items) + $v
                Save-PcConfig $cfg
                Write-PcLog "$Title — добавлено: $v"
            }
        }
        Start-ScheduledTask -TaskPath $PcTaskPath -TaskName 'Enforcer' -ErrorAction SilentlyContinue
    }
}

function Show-RunningApps {
    $cfg = Get-PcConfig
    $session = Get-PcChildSession $cfg.childUser
    if (-not $session) { Write-Host 'Ребёнок сейчас не в системе.'; return }
    Write-Host 'Программы с окнами, запущенные ребёнком (имя для блокировки — в первой колонке):'
    $session.Processes | Where-Object { $_.MainWindowTitle } |
        Sort-Object ProcessName -Unique |
        Format-Table @{ n = 'Имя процесса'; e = { $_.ProcessName } }, @{ n = 'Заголовок окна'; e = { $_.MainWindowTitle } } -AutoSize |
        Out-Host
}

function Show-Log {
    if (Test-Path $LogPath) { Get-Content $LogPath -Encoding UTF8 -Tail 30 | Out-Host } else { Write-Host 'Журнал пуст.' }
}

function Repair-Tasks {
    $cfg = Get-PcConfig
    Register-PcTasks $cfg.childUser
    Start-ScheduledTask -TaskPath $PcTaskPath -TaskName 'Enforcer'
    Write-PcLog 'Задачи планировщика восстановлены'
    Write-Host 'Готово.' -ForegroundColor Green
}

function Uninstall-Pc {
    if (-not (Read-YesNo 'Полностью удалить родительский контроль с этого компьютера?')) { return $false }
    Unregister-PcTasks
    try { [void](Update-PcHosts @()) } catch { Write-Host "Не удалось очистить hosts: $($_.Exception.Message)" -ForegroundColor Yellow }
    Set-Location $env:SystemRoot
    Remove-Item -Path $PcRoot -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $PcRoot) {
        Write-Host "Задачи удалены. Папку $PcRoot удалите вручную." -ForegroundColor Yellow
    } else {
        Write-Host 'Родительский контроль удалён.' -ForegroundColor Green
    }
    return $true
}

# ---------- Главное меню ----------
while ($true) {
    try {
        Clear-Host
        Write-Host '=== Родительский контроль: панель родителя ===' -ForegroundColor Cyan
        Show-Status
        Write-Host ''
        Write-Host '  1. Добавить или убавить время на сегодня'
        Write-Host '  2. Заблокировать до конца дня / снять блокировку'
        Write-Host '  3. Изменить дневные лимиты'
        Write-Host '  4. Изменить разрешённые часы'
        Write-Host '  5. Заблокированные сайты'
        Write-Host '  6. Заблокированные программы'
        Write-Host '  7. Что сейчас запущено у ребёнка'
        Write-Host '  8. Журнал событий'
        Write-Host '  9. Восстановить (если контроль не работает)'
        Write-Host '  U. Удалить программу'
        Write-Host '  0. Выход'
        $c = Read-Host 'Выберите пункт'
        $pause = $true
        switch -Regex ($c) {
            '^1$' { Add-Bonus }
            '^2$' { Set-BlockToday }
            '^3$' { Set-Limits }
            '^4$' { Set-Hours }
            '^5$' { Edit-List 'blockedSites' 'Заблокированные сайты' 'youtube.com'; $pause = $false }
            '^6$' { Edit-List 'blockedApps' 'Заблокированные программы' 'RobloxPlayerBeta или Minecraft*'; $pause = $false }
            '^7$' { Show-RunningApps }
            '^8$' { Show-Log }
            '^9$' { Repair-Tasks }
            '^[uUуУ]$' { if (Uninstall-Pc) { Read-Host 'Нажмите Enter' | Out-Null; exit 0 } }
            '^0$' { exit 0 }
            default { $pause = $false }
        }
        if ($pause) { Read-Host 'Нажмите Enter, чтобы продолжить' | Out-Null }
    } catch {
        Write-Host "ОШИБКА: $($_.Exception.Message)" -ForegroundColor Red
        Read-Host 'Нажмите Enter, чтобы продолжить' | Out-Null
    }
}
