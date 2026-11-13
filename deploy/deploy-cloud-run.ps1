<#
Publica a API de Devoluções no Google Cloud Run (São Paulo).

Uso (na pasta devolucoes-api, depois de `gcloud auth login`):
    .\deploy\deploy-cloud-run.ps1 -ProjectId SEU-PROJETO

O que faz:
  1. Ativa os serviços necessários no projeto
  2. Guarda as credenciais do .env no Secret Manager (sem exibir valores)
  3. Cria o bucket privado das fotos/vídeos e copia as mídias deste PC para ele
  4. Publica a API no Cloud Run (máx. 1 instância, escala a zero) com MEDIA_BACKEND=gcs
  5. Cria o agendamento de 15 em 15 minutos (Cloud Scheduler -> POST /jobs/run)
Pode ser rodado de novo para publicar atualizações.
#>
param(
    [Parameter(Mandatory = $true)][string]$ProjectId,
    [string]$Region = "southamerica-east1",
    [string]$Service = "devolucoes-api",
    [string]$Schedule = "*/15 * * * *",
    [string]$Bucket = "",
    [switch]$SkipMediaMigration
)
if (-not $Bucket) { $Bucket = "$ProjectId-devolucoes-midias" }

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

# Credenciais que vão para o Secret Manager (nunca como variável de ambiente comum)
$SecretKeys = @("DATABASE_URL", "API_KEY", "TIKTOK_APP_SECRET", "OLIST_API_TOKEN")
# Opcionais: vão para o Secret Manager só se estiverem preenchidas
$OptionalSecretKeys = @("SHOPEE_PARTNER_KEY", "MERCADOLIVRE_CLIENT_SECRET", "SMTP_PASSWORD", "TOKEN_ENCRYPTION_KEY")
# Variáveis que não fazem sentido no servidor
$SkipKeys = @("ENABLE_SCHEDULER", "WEBHOOK_PROCESS_INLINE", "PUBLIC_BASE_URL", "APP_URL",
    "MEDIA_BACKEND", "GCS_BUCKET", "MEDIA_DIR")

function Read-DotEnv([string]$Path) {
    $vars = [ordered]@{}
    foreach ($line in Get-Content $Path -Encoding UTF8) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith("#") -or -not $t.Contains("=")) { continue }
        $k, $v = $t.Split("=", 2)
        $v = ($v -replace '\s+#.*$', '').Trim()
        if ($v) { $vars[$k.Trim()] = $v }
    }
    return $vars
}

Write-Host "`n== Projeto $ProjectId / região $Region ==" -ForegroundColor Cyan
gcloud config set project $ProjectId | Out-Null

Write-Host "`n[1/5] Ativando serviços..." -ForegroundColor Cyan
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com `
    secretmanager.googleapis.com cloudscheduler.googleapis.com storage.googleapis.com

$envVars = Read-DotEnv ".env"
foreach ($k in $SecretKeys) {
    if (-not $envVars.Contains($k)) { throw "$k não está preenchido no .env" }
}
$SecretKeys += @($OptionalSecretKeys | Where-Object { $envVars.Contains($_) })

$projectNumber = gcloud projects describe $ProjectId --format="value(projectNumber)"
$runAccount = "$projectNumber-compute@developer.gserviceaccount.com"

Write-Host "`n[2/5] Bucket das fotos e vídeos (Cloud Storage)..." -ForegroundColor Cyan
# Privado (sem acesso público); só a API, pela conta de serviço do Cloud Run, lê e grava
$bucketUri = "gs://$Bucket"
gcloud storage buckets describe $bucketUri 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    gcloud storage buckets create $bucketUri --location $Region --uniform-bucket-level-access `
        --public-access-prevention | Out-Null
    Write-Host "  criado: $bucketUri"
} else {
    Write-Host "  já existe: $bucketUri"
}
gcloud storage buckets add-iam-policy-binding $bucketUri --member="serviceAccount:$runAccount" `
    --role="roles/storage.objectAdmin" | Out-Null
if (-not $SkipMediaMigration -and (Test-Path "media")) {
    Write-Host "  copiando as fotos/vídeos deste computador para o bucket..."
    & ".venv\Scripts\python.exe" scripts\migrar_midias_gcs.py $Bucket
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao copiar as mídias. Rode 'gcloud auth application-default login' e tente de novo (ou use -SkipMediaMigration)."
    }
}

Write-Host "`n[3/5] Guardando credenciais no Secret Manager..." -ForegroundColor Cyan
$secretFlags = @()
foreach ($k in $SecretKeys) {
    $name = "devolucoes-" + $k.ToLower().Replace("_", "-")
    $tmp = New-TemporaryFile
    try {
        [IO.File]::WriteAllText($tmp, $envVars[$k])  # sem quebra de linha e sem BOM
        $exists = gcloud secrets list --filter="name~$name$" --format="value(name)"
        if ($exists) {
            gcloud secrets versions add $name --data-file="$tmp" | Out-Null
        } else {
            gcloud secrets create $name --replication-policy=automatic --data-file="$tmp" | Out-Null
        }
    } finally { Remove-Item $tmp -Force }
    gcloud secrets add-iam-policy-binding $name --member="serviceAccount:$runAccount" `
        --role="roles/secretmanager.secretAccessor" | Out-Null
    $secretFlags += "$k=${name}:latest"
    Write-Host "  ok: $k"
}

# Demais variáveis do .env vão como variáveis de ambiente comuns
$yaml = New-TemporaryFile
$lines = @("ENABLE_SCHEDULER: 'false'", "WEBHOOK_PROCESS_INLINE: 'true'", "MEDIA_BACKEND: 'gcs'", "GCS_BUCKET: '$Bucket'")
foreach ($k in $envVars.Keys) {
    if ($SecretKeys -contains $k -or $SkipKeys -contains $k) { continue }
    $lines += "${k}: '" + $envVars[$k].Replace("'", "''") + "'"
}
[IO.File]::WriteAllLines($yaml, $lines)

Write-Host "`n[4/5] Publicando no Cloud Run (leva alguns minutos)..." -ForegroundColor Cyan
try {
    gcloud run deploy $Service --source . --region $Region `
        --allow-unauthenticated `
        --min-instances 0 --max-instances 1 `
        --cpu 1 --memory 512Mi --concurrency 20 --timeout 900 `
        --env-vars-file "$yaml" `
        --set-secrets ($secretFlags -join ",")
} finally { Remove-Item $yaml -Force }

$url = gcloud run services describe $Service --region $Region --format="value(status.url)"
gcloud run services update $Service --region $Region --update-env-vars "PUBLIC_BASE_URL=$url,APP_URL=$url" | Out-Null

Write-Host "`n[5/5] Agendando rotinas a cada 15 minutos..." -ForegroundColor Cyan
$job = "$Service-rotinas"
$headers = "X-API-Key=$($envVars['API_KEY'])"
$existing = gcloud scheduler jobs list --location $Region --filter="name~$job$" --format="value(name)"
$verb = if ($existing) { "update" } else { "create" }
gcloud scheduler jobs $verb http $job --location $Region --schedule "$Schedule" --time-zone "America/Sao_Paulo" `
    --uri "$url/jobs/run" --http-method POST --headers $headers --attempt-deadline 900s | Out-Null

Write-Host "`n== Pronto ==" -ForegroundColor Green
Write-Host "API:      $url"
Write-Host "Saúde:    $url/health"
Write-Host "Docs:     $url/docs"
Write-Host "Webhook:  $url/webhooks/tiktok   (cadastrar no Partner Center)"
Write-Host "Callback: $url/auth/tiktok/callback   (URL de redirecionamento do app)"
Write-Host "Telas:    $url/app"
Write-Host "Mídias:   gs://$Bucket"
Write-Host "`nDesligue as tarefas provisórias deste PC para não rodar em dobro:" -ForegroundColor Yellow
Write-Host "  .\deploy\agendar-rotinas-windows.ps1 -Remover"
Write-Host "  .\deploy\agendar-servidor-windows.ps1 -Remover"
