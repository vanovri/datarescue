param([ValidateRange(1024,65535)][int]$Port = 8763)
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw 'Install Python 3.11+ first.' }
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw 'Install Node.js 22.12+ or 24+ first.' }
    $projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $projectPython)) {
        python -m venv (Join-Path $PSScriptRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
    }
    & $projectPython -m pip install -c constraints.txt -e '.[dev]'
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
    Push-Location -LiteralPath (Join-Path $PSScriptRoot 'frontend')
    try {
        npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
        npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    } finally { Pop-Location }
    Write-Host "DataRescue: http://127.0.0.1:$Port"
    Write-Host 'Press Ctrl+C to stop the local server.'
    & $projectPython -m uvicorn datarescue.api:app --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) { throw 'Local server stopped with an error. Check whether the port is already in use.' }
} finally { Pop-Location }
