<#
Solução provisória enquanto a API não está no Cloud Run: agenda scripts\rotinas.py no
Agendador de Tarefas do Windows, a cada 15 minutos, enquanto o usuário estiver logado.

    .\deploy\agendar-rotinas-windows.ps1            # cria/atualiza a tarefa
    .\deploy\agendar-rotinas-windows.ps1 -Remover   # apaga a tarefa (ex.: depois do deploy)

Log: logs\rotinas.log
#>
param([switch]$Remover)

$ErrorActionPreference = "Stop"
$TaskName = "Devolucoes - rotinas a cada 15 min"
$Root = Split-Path $PSScriptRoot -Parent

if ($Remover) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Tarefa '$TaskName' removida."
    return
}

# pythonw: roda sem abrir janela de console
$Python = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Python)) { throw "Não achei $Python. Crie o ambiente: python -m venv .venv" }

$action = New-ScheduledTaskAction -Execute $Python -Argument "scripts\rotinas.py" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Sincroniza devoluções (TikTok, Shopee, Mercado Livre), renova tokens e acompanha o Olist. Log em $Root\logs\rotinas.log" `
    -Force | Out-Null

Write-Host "Tarefa '$TaskName' agendada a cada 15 minutos. Log: $Root\logs\rotinas.log"
