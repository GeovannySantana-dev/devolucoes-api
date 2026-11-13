<#
Deixa as telas (/app) no ar neste computador, na porta 8000 só local (127.0.0.1). O acesso de fora é pelo
endereço https do Tailscale Funnel. Inicia ao entrar no Windows e já sobe agora.

    .\deploy\agendar-servidor-windows.ps1            # cria/atualiza e inicia
    .\deploy\agendar-servidor-windows.ps1 -Remover   # para e apaga a tarefa (ex.: depois do deploy)

Log: logs\servidor.log. Para liberar no Wi-Fi (sem Funnel), defina HOST=0.0.0.0 e libere a porta no Firewall.
#>
param([switch]$Remover)

$ErrorActionPreference = "Stop"
$TaskName = "Devolucoes - servidor das telas"
$Root = Split-Path $PSScriptRoot -Parent

if ($Remover) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Tarefa '$TaskName' removida."
    return
}

$Python = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Python)) { throw "Não achei $Python. Crie o ambiente: python -m venv .venv" }

$action = New-ScheduledTaskAction -Execute $Python -Argument "scripts\servidor.py" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Telas de devoluções (porta 8000 local, publicada pelo Tailscale Funnel). Log em $Root\logs\servidor.log" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Servidor agendado e iniciado. Acesso pelo endereço https do Tailscale Funnel."
