param(
    [Parameter(Mandatory = $true)]
    [string]$Python
)

$ErrorActionPreference = 'Stop'
$taskName = 'Qoder.StockTrading.GO_BOT'
$bot = Join-Path $PSScriptRoot 'bot.py'
$state = Join-Path $PSScriptRoot 'state.json'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'Python executable was not found.'
}
if (-not (Test-Path -LiteralPath $state -PathType Leaf)) {
    throw 'Initialize the isolated runtime with bot.py --init first.'
}
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw 'The task already exists; it will not be overwritten.'
}
$action = New-ScheduledTaskAction -Execute $Python -Argument ('-X utf8 "' + $bot + '" --tick') -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Versioned stock research, independent paper ledger and Feishu reports; no broker orders.' | Select-Object TaskName, State
