$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3 -m venv .venv
        } else {
            & python -m venv .venv
        }
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required.' }
    }
    & '.venv/Scripts/python.exe' -m pip install -e '.[dev]'
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & '.venv/Scripts/python.exe' scripts/setup_ocr.py
    if ($LASTEXITCODE -ne 0) { throw 'OCR model setup failed.' }
    Write-Host 'Ready. Run Start.ps1 or double-click start.cmd.'
} finally {
    Pop-Location
}
