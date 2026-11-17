#Requires -RunAsAdministrator
# Установка программы "Родительский контроль"

$ErrorActionPreference = 'Stop'
$Src = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $Src 'common.ps1')

function Read-Default([string]$Prompt, [string]$Default) {
    $v = Read-Host "$Prompt [Enter = $Default]"
    if ([string]::IsNullOrWhiteSpace($v)) { return $Default }
    return $v.Trim()
}

function Read-Minutes([string]$Prompt, [int]$Default) {
    while ($true) {
        $v = Read-Default $Prompt "$Default"
        $n = 0
        if ([int]::TryParse($v, [ref]$n) -and $n -ge 0 -and $n -le 1440) { return $n }
        Write-Host '  Введите число минут от 0 до 1440.' -ForegroundColor Yellow
    }
}

function Read-Hours([string]$Title, [string]$DefFrom, [string]$DefTo) {
    while ($true) {
        $f = Read-Default "  $Title — с (ЧЧ:ММ)" $DefFrom
        $t = Read-Default "  $Title — до (ЧЧ:ММ, можно 24:00)" $DefTo
        try {
            if ((ConvertTo-PcTime $f) -lt (ConvertTo-PcTime $t)) { return [pscustomobject]@{ from = $f; to = $t } }
            Write-Host '  Время "с" должно быть раньше времени "до".' -ForegroundColor Yellow
        } catch { Write-Host "  $($_.Exception.Message)" -ForegroundColor Yellow }
    }
}

try {
    Write-Host ''
    Write-Host '=== Установка родительского контроля ===' -ForegroundColor Cyan
    Write-Host ''

    # --- Выбор учётной записи ребёнка ---
    $adminNames = @()
    try {
        $adminNames = @(Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop | ForEach-Object { ($_.Name -split '\\')[-1] })
    } catch {
        Write-Host 'Не удалось определить, у кого есть права администратора. Проверьте это вручную в Параметрах.' -ForegroundColor Yellow
    }

    $users = @(Get-LocalUser | Where-Object { $_.Enabled } | Sort-Object Name)
    if ($users.Count -eq 0) { throw 'Не найдено ни одной включённой учётной записи.' }

    Write-Host 'Учётные записи на этом компьютере:'
    for ($i = 0; $i -lt $users.Count; $i++) {
        $u = $users[$i]
        $mark = ''
        if ($adminNames -contains $u.Name) { $mark = '  [администратор]' }
        if ($u.Name -eq $env:USERNAME) { $mark += '  [это вы]' }
        Write-Host ("  {0}. {1}{2}" -f ($i + 1), $u.Name, $mark)
    }
    Write-Host ''

    $child = $null
    while (-not $child) {
        $v = Read-Host 'Номер учётной записи РЕБЁНКА'
        $n = 0
        if ([int]::TryParse($v, [ref]$n) -and $n -ge 1 -and $n -le $users.Count) { $child = $users[$n - 1].Name }
    }

    if ($child -eq $env:USERNAME) {
        throw 'Нельзя выбрать собственную учётную запись. Устанавливайте программу из учётной записи родителя (администратора).'
    }

    if ($adminNames -contains $child) {
        Write-Host ''
        Write-Host "ВНИМАНИЕ: у '$child' есть права администратора — тогда ребёнок сможет отключить контроль." -ForegroundColor Yellow
        $ans = Read-Host 'Сделать эту учётную запись обычной (без прав администратора)? (д/н)'
        if ($ans -match '^(д|y|да|yes)$') {
            Remove-LocalGroupMember -SID 'S-1-5-32-544' -Member $child
            Write-Host "  Права администратора у '$child' сняты. Изменение вступит в силу после следующего входа." -ForegroundColor Green
        } else {
            Write-Host '  Хорошо, оставляю как есть. Защита будет ненадёжной.' -ForegroundColor Yellow
        }
    }

    # --- Настройки ---
    $cfg = $null
    if (Test-Path $ConfigPath) {
        $ans = Read-Host 'Найдены прежние настройки. Сохранить их? (д/н)'
        if ($ans -match '^(д|y|да|yes)$') { $cfg = Get-PcConfig }
    }
    if (-not $cfg) {
        $cfg = New-PcDefaultConfig
        Write-Host ''
        Write-Host 'Лимит времени за компьютером в день (в минутах):'
        $cfg.limits.weekdays = Read-Minutes '  Будни (пн–пт)' 120
        $cfg.limits.weekends = Read-Minutes '  Выходные (сб–вс)' 180
        Write-Host ''
        Write-Host 'Разрешённые часы:'
        $cfg.hours.weekdays = Read-Hours 'Будни' '08:00' '21:00'
        $cfg.hours.weekends = Read-Hours 'Выходные' '09:00' '22:00'
    }
    $cfg.childUser = $child

    # --- Копирование файлов и защита папки ---
    New-Item -ItemType Directory -Path $PcRoot -Force | Out-Null
    foreach ($f in 'common.ps1', 'enforcer.ps1', 'notify.ps1', 'parent.ps1') {
        Copy-Item -Path (Join-Path $Src $f) -Destination (Join-Path $PcRoot $f) -Force
    }
    # Полный доступ: SYSTEM и Администраторы; обычные пользователи — только чтение
    & icacls.exe $PcRoot /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' /T /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось настроить права доступа к папке программы.' }

    Save-PcConfig $cfg

    # --- Задачи планировщика ---
    Register-PcTasks $child
    Start-ScheduledTask -TaskPath $PcTaskPath -TaskName 'Enforcer'
    Write-PcLog "Установлено. Ребёнок: $child"

    Write-Host ''
    Write-Host 'Готово! Родительский контроль установлен и уже работает.' -ForegroundColor Green
    Write-Host "  Ребёнок: $child"
    Write-Host "  Будни: $($cfg.limits.weekdays) мин в день, с $($cfg.hours.weekdays.from) до $($cfg.hours.weekdays.to)"
    Write-Host "  Выходные: $($cfg.limits.weekends) мин в день, с $($cfg.hours.weekends.from) до $($cfg.hours.weekends.to)"
    Write-Host ''
    Write-Host 'Чтобы изменить настройки, добавить время или заблокировать сайты и программы, запустите parent.cmd.'
    Write-Host 'Важно: у вашей учётной записи администратора должен быть пароль, которого ребёнок не знает.' -ForegroundColor Yellow
} catch {
    Write-Host ''
    Write-Host "ОШИБКА: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ''
Read-Host 'Нажмите Enter, чтобы закрыть окно' | Out-Null
