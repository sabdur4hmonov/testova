param(
    [ValidateRange(1024,65535)][int]$Port = 8010,
    [switch]$EnableCollection,
    [switch]$AutoLearn
)
$ErrorActionPreference = "Stop"
$detectorRoot = $PSScriptRoot
$projectRoot = Split-Path -Parent $detectorRoot
$pythonPath = Join-Path $detectorRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) { throw "detector/.venv Python topilmadi. detector/README.md ni o'qing." }
foreach ($modelName in @("uz-latin-linear","uz-cyrillic-linear","ru-ensemble","en-tmr")) {
    if (-not (Test-Path -LiteralPath (Join-Path $detectorRoot "models\$modelName\detector_meta.json"))) { throw "Model topilmadi: $modelName" }
}
$env:DETECTOR_MODELS_DIR = Join-Path $detectorRoot "models"
$env:DETECTOR_LANGUAGES = "uz,uz-cyrl,ru,en"
$env:DETECTOR_THREADS = "2"
$env:OMP_NUM_THREADS = "2"
$env:OPENBLAS_NUM_THREADS = "2"
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:DETECTOR_COLLECTION_ENABLED = if ($EnableCollection -or $AutoLearn) { "true" } else { "false" }
$env:DETECTOR_AUTO_LEARN_ENABLED = if ($AutoLearn) { "true" } else { "false" }
$env:DETECTOR_CORPUS_DB = Join-Path $detectorRoot "data\feedback\corpus.sqlite3"
$env:DETECTOR_RELEASES_DIR = Join-Path $detectorRoot "data\model_updates\releases"
$learner = $null
Write-Host "Testova detektor: http://127.0.0.1:$Port"
Write-Host "To'xtatish: Ctrl+C. Matnlar tashqi APIga yuborilmaydi."
Push-Location $projectRoot
try {
    if ($AutoLearn) {
        $logDir = Join-Path $detectorRoot "data\work\model_updates"
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        $learner = Start-Process -FilePath $pythonPath -ArgumentList @("-B","-u","-m","detector.training.continuous","watch","--auto-promote","--interval","60") -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDir "worker_stdout.log") -RedirectStandardError (Join-Path $logDir "worker_stderr.log")
        Write-Host "O'rganuvchi faqat tekshirilgan namunalar va qabul sinovidan o'tgan yangi versiya bilan ishlaydi."
    }
    & $pythonPath -B -m uvicorn detector.service.api:app --host 127.0.0.1 --port $Port --workers 1
    if ($LASTEXITCODE -ne 0) { throw "Detektor ishga tushmadi." }
} finally {
    if ($null -ne $learner) {
        # venv launcher may own a second Python process: terminate only its children.
        $owned = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($learner.Id)" -ErrorAction SilentlyContinue
        foreach ($child in $owned) { Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue }
        Stop-Process -Id $learner.Id -ErrorAction SilentlyContinue
    }
    Pop-Location
}
