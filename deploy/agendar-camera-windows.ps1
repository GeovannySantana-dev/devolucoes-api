<#
Câmera da bancada: deixa a gravação contínua rodando neste computador (o servidor).
Inicia ao entrar no Windows, reinicia se cair, e já sobe agora.

Antes: coloque no .env o endereço RTSP da câmera (CAMERA_RTSP_URL=rtsp://usuario:senha@IP:554/...)
e tenha o ffmpeg instalado (este script instala pelo winget, se houver; senão, informe FFMPEG_PATH no .env).

    .\deploy\agendar-camera-windows.ps1            # cria/atualiza e inicia
    .\deploy\agendar-camera-windows.ps1 -Remover   # para e apaga a tarefa

Log: logs\camera.log. Gravações: pasta "gravacoes" (ou CAMERA_DIR), apagadas sozinhas após CAMERA_RETENTION_DAYS.
#>
param([switch]$Remover)

$ErrorActionPreference = "Stop"
$TaskName = "Devolucoes - camera da bancada"
$Root = Split-Path $PSScriptRoot -Parent

if ($Remover) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Tarefa '$TaskName' removida."
    return
}

$envFile = Join-Path $Root ".env"
if (-not (Select-String -Path $envFile -Pattern '^CAMERA_RTSP_URL=.+' -Quiet)) {
    throw "Preencha CAMERA_RTSP_URL no .env antes (endereço RTSP da câmera)."
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -and -not (Select-String -Path $envFile -Pattern '^FFMPEG_PATH=.+' -Quiet)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
        Write-Host "ffmpeg instalado. Feche e abra o PowerShell e rode este script de novo." -ForegroundColor Yellow
        return
    }
    throw "ffmpeg não encontrado. Instale (https://www.gyan.dev/ffmpeg/builds/) e informe FFMPEG_PATH no .env."
}

$Python = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Python)) { throw "Não achei $Python. Rode antes .\deploy\instalar-servidor-windows.ps1" }

$action = New-ScheduledTaskAction -Execute $Python -Argument "scripts\camera_gravar.py" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Grava a câmera da bancada em arquivos de 1 minuto. Log em $Root\logs\camera.log" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Gravação da câmera agendada e iniciada. Confira logs\camera.log e a pasta de gravações em 1-2 minutos."
