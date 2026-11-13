<#
Gera um .zip da pasta do app para levar ao computador servidor (pendrive ou rede).

    .\deploy\empacotar-para-servidor.ps1        # cria devolucoes-servidor.zip na Área de Trabalho

Leva o código, o .env (credenciais!), as fotos/vídeos (media) e os backups. Deixa de fora o .venv
(é recriado no servidor), logs e caches. Depois de copiar, apague o .zip do pendrive: o .env tem senhas.
#>
param([string]$Destino = (Join-Path ([Environment]::GetFolderPath("Desktop")) "devolucoes-servidor.zip"))

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$Skip = @(".venv", ".pytest_cache", "logs", ".claude", ".vscode")

$tmp = Join-Path $env:TEMP "devolucoes-servidor"
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
$dest = Join-Path $tmp "devolucoes-api"
New-Item -ItemType Directory -Force $dest | Out-Null
Get-ChildItem $Root -Force | Where-Object { $Skip -notcontains $_.Name } |
    ForEach-Object { Copy-Item $_.FullName -Destination $dest -Recurse -Force }
Get-ChildItem $dest -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

if (Test-Path $Destino) { Remove-Item $Destino -Force }
Compress-Archive -Path $dest -DestinationPath $Destino
Remove-Item $tmp -Recurse -Force
$mb = [Math]::Round((Get-Item $Destino).Length / 1MB, 1)
Write-Host "Pacote criado: $Destino ($mb MB)" -ForegroundColor Green
Write-Host "Atenção: tem o .env com as senhas. Apague do pendrive depois de copiar." -ForegroundColor Yellow
