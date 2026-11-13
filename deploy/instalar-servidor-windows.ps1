<#
Transforma este computador no servidor do app de devoluções (até a ida para a nuvem):

  1. Instala o Python 3.12 e o Tailscale (pelo winget), se faltarem
  2. Cria o ambiente .venv e instala as dependências
  3. Agenda as rotinas (a cada 15 min) e o servidor das telas (ao entrar no Windows)
  4. Publica o app na internet com HTTPS e endereço fixo pelo Tailscale Funnel
  5. Grava o endereço novo no .env (PUBLIC_BASE_URL e APP_URL) e confere se abre

Uso (PowerShell, na pasta devolucoes-api copiada para este PC, já com o .env):
    powershell -ExecutionPolicy Bypass -File .\deploy\instalar-servidor-windows.ps1

Pode ser rodado de novo sem problema (por exemplo, depois de atualizar os arquivos do app).
Para tirar do ar:  .\deploy\instalar-servidor-windows.ps1 -Remover
#>
param([switch]$Remover, [int]$Port = 8000)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
$TailscaleExe = Join-Path $env:ProgramFiles "Tailscale\tailscale.exe"

function Step($text) { Write-Host "`n$text" -ForegroundColor Cyan }

function Set-DotEnv([string]$Key, [string]$Value) {
    $path = Join-Path $Root ".env"
    $lines = [Collections.Generic.List[string]]([IO.File]::ReadAllLines($path))
    $i = $lines.FindIndex({ param($l) $l -match "^\s*$Key\s*=" })
    if ($i -ge 0) { $lines[$i] = "$Key=$Value" } else { $lines.Add("$Key=$Value") }
    [IO.File]::WriteAllLines($path, $lines, [Text.UTF8Encoding]::new($false))
}

if ($Remover) {
    if (Test-Path $TailscaleExe) { & $TailscaleExe funnel reset 2>$null }
    & "$PSScriptRoot\agendar-servidor-windows.ps1" -Remover
    & "$PSScriptRoot\agendar-rotinas-windows.ps1" -Remover
    Write-Host "App tirado do ar neste computador (o Tailscale continua instalado)."
    return
}

if (-not (Test-Path ".env")) { throw "Falta o arquivo .env nesta pasta. Copie o .env do computador antigo." }

Step "[1/5] Python 3.12..."
$py = $null
try { $py = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null) } catch {}
if (-not $py) {
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
    $py = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (-not (Test-Path $py)) { throw "O Python foi instalado, mas não achei o python.exe. Feche e abra o PowerShell e rode de novo." }
}
Write-Host "  ok: $py"

Step "[2/5] Ambiente do app (.venv)..."
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
$broken = $false
if (Test-Path $venvPy) { try { $broken = -not (& $venvPy -c "print(1)" 2>$null) } catch { $broken = $true } }
if ($broken) { Remove-Item ".venv" -Recurse -Force }  # .venv copiado de outro PC não funciona
if (-not (Test-Path $venvPy)) { & $py -m venv .venv }
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar as dependências." }
& $venvPy -c "import app.main" ; if ($LASTEXITCODE -ne 0) { throw "O app não abriu. Confira o .env." }
Write-Host "  ok"

Step "[3/5] Tarefas agendadas (rotinas e servidor)..."
& "$PSScriptRoot\agendar-rotinas-windows.ps1"
& "$PSScriptRoot\agendar-servidor-windows.ps1"

Step "[4/5] Tailscale Funnel (endereço público com HTTPS)..."
if (-not (Test-Path $TailscaleExe)) {
    winget install --id Tailscale.Tailscale -e --accept-source-agreements --accept-package-agreements
    Start-Sleep 5
}
$status = & $TailscaleExe status --json | ConvertFrom-Json
if ($status.BackendState -ne "Running") {
    Write-Host "  Vai abrir o navegador: entre na sua conta do Tailscale e autorize este computador." -ForegroundColor Yellow
    & $TailscaleExe up
    $status = & $TailscaleExe status --json | ConvertFrom-Json
}
Write-Host "  Se aparecer um link pedindo para ativar o Funnel, abra, clique em ativar e espere aqui." -ForegroundColor Yellow
& $TailscaleExe funnel --bg $Port
if ($LASTEXITCODE -ne 0) { throw "O Funnel não foi ativado. Rode o script de novo depois de ativar pelo link." }
$dns = $status.Self.DNSName.TrimEnd(".")
if (-not $dns) { throw "Não consegui o endereço do Tailscale (tailscale status)." }
$url = "https://$dns"

Step "[5/5] Gravando o endereço no .env e conferindo..."
Set-DotEnv "PUBLIC_BASE_URL" $url
Set-DotEnv "APP_URL" $url
Stop-ScheduledTask -TaskName "Devolucoes - servidor das telas" -ErrorAction SilentlyContinue
Start-Sleep 2
Start-ScheduledTask -TaskName "Devolucoes - servidor das telas"
$ok = $false
foreach ($i in 1..20) {
    Start-Sleep 3
    try { if ((Invoke-WebRequest "$url/health" -UseBasicParsing -TimeoutSec 10).StatusCode -eq 200) { $ok = $true; break } } catch {}
}
if ($ok) { Write-Host "  ok: $url/health respondeu" -ForegroundColor Green }
else { Write-Host "  Ainda não respondeu pela internet (o certificado pode levar 1-2 min na primeira vez). Teste: $url/health" -ForegroundColor Yellow }

Write-Host "`n== Pronto ==" -ForegroundColor Green
Write-Host "Telas:     $url/app      (celular, de qualquer lugar)"
Write-Host "Webhooks:  $url/webhooks/tiktok   $url/webhooks/mercadolivre"
Write-Host "Callbacks: $url/auth/tiktok/callback   $url/auth/shopee/callback"
Write-Host "`nDeixe este computador sempre ligado, sem suspender, e com o usuário logado." -ForegroundColor Yellow
Write-Host "No computador antigo, desligue as tarefas para não rodar em dobro:" -ForegroundColor Yellow
Write-Host "  .\deploy\instalar-servidor-windows.ps1 -Remover"
