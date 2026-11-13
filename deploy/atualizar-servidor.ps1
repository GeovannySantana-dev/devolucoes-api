<#
Atualiza o app no computador servidor com a versão mais nova do GitHub e reinicia as telas.

Primeira vez (a pasta ainda não está ligada ao GitHub):
    powershell -ExecutionPolicy Bypass -File .\deploy\atualizar-servidor.ps1 -Repo https://github.com/CONTA/devolucoes-api.git
Depois:
    powershell -ExecutionPolicy Bypass -File .\deploy\atualizar-servidor.ps1

O .env, as fotos (media), os backups e os logs não estão no GitHub e não são tocados.
O código do servidor fica igual ao do GitHub: não edite arquivos do app direto no servidor.
#>
param([string]$Repo = "", [string]$Branch = "main")

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
$TaskName = "Devolucoes - servidor das telas"

function Step($text) { Write-Host "`n$text" -ForegroundColor Cyan }
function Invoke-Git {
    # git escreve avisos no stderr; no PowerShell 5.1 isso não pode virar erro fatal
    $ErrorActionPreference = "Continue"
    & git.exe @args 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') falhou" }
}

Step "[1/4] Git..."
if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw "Falta o Git. Instale pelo site https://git-scm.com/download/win e rode de novo." }
    winget install --id Git.Git -e --silent --accept-source-agreements --accept-package-agreements
    $env:Path += ";$env:ProgramFiles\Git\cmd"
    if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) { throw "Instalei o Git, mas ele não apareceu. Feche e abra o PowerShell e rode de novo." }
}
if (-not (Test-Path ".git")) {
    if (-not $Repo) { throw "Primeira vez: informe o endereço do repositório com -Repo https://github.com/CONTA/devolucoes-api.git" }
    Invoke-Git init -q
    Invoke-Git remote add origin $Repo
} elseif ($Repo) {
    Invoke-Git remote set-url origin $Repo
}

Step "[2/4] Buscando a versão nova no GitHub..."
# Na primeira vez o Windows pode abrir o navegador para entrar no GitHub (é só autorizar).
Invoke-Git fetch -q origin $Branch
$before = (& git.exe rev-parse -q --verify HEAD 2>$null)
$changed = if ($before) { & git.exe diff --name-only $before "origin/$Branch" } else { @() }
Invoke-Git reset -q --hard "origin/$Branch"
$after = (& git.exe log -1 --format="%h %s (%cr)")
Write-Host "  versão: $after"
if ($before -and -not $changed) { Write-Host "  já estava na versão mais nova." }

Step "[3/4] Dependências..."
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "Falta o .venv. Rode antes .\deploy\instalar-servidor-windows.ps1" }
& {
    # Como no git: o aviso do pip no stderr ("new release of pip") parava o script na tarefa agendada
    $ErrorActionPreference = "Continue"
    & $venvPy -m pip install --quiet --disable-pip-version-check -r requirements.txt 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar as dependências." }
    & $venvPy -c "import app.main" 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) { throw "O app não abriu com a versão nova. Confira o .env." }
}

Step "[4/4] Reiniciando as telas..."
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep 2
Start-ScheduledTask -TaskName $TaskName
$ok = $false
foreach ($i in 1..15) {
    Start-Sleep 2
    try { if ((Invoke-WebRequest "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200) { $ok = $true; break } } catch {}
}
if ($ok) { Write-Host "  ok: o app voltou" -ForegroundColor Green } else { throw "O app não voltou. Veja logs\servidor.log" }

$migrations = @($changed | Where-Object { $_ -like "supabase/migrations/*" })
if ($migrations) {
    Write-Host "`nAtenção: há mudanças no banco para aplicar no Supabase:" -ForegroundColor Yellow
    $migrations | ForEach-Object { Write-Host "  $_" }
}
Write-Host "`n== Atualizado ==" -ForegroundColor Green
