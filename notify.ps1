# Показывает ребёнку всплывающее сообщение. Запускается от имени ребёнка задачей Notify.
$file = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'notify.txt'
if (-not (Test-Path $file)) { exit 0 }
$text = (Get-Content -Path $file -Raw -Encoding UTF8).Trim()
if (-not $text) { exit 0 }
$title = 'Родительский контроль'
try {
    $shell = New-Object -ComObject WScript.Shell
    # 48 = значок предупреждения, 4096 = поверх всех окон; окно закроется само через 45 секунд
    [void]$shell.Popup($text, 45, $title, 48 + 4096)
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [void][System.Windows.Forms.MessageBox]::Show($text, $title, 'OK', 'Warning')
}
