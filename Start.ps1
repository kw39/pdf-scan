param([ValidateRange(1024, 65535)][int]$Port = 8765)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
        throw 'Run Setup.ps1 first.'
    }
    $scanUrl = "http://127.0.0.1:$Port"
    $scanExisting = $null
    try { $scanExisting = Invoke-RestMethod -Uri "$scanUrl/api/config" -TimeoutSec 2 } catch {}
    if ($scanExisting -and $scanExisting.version -and $scanExisting.PSObject.Properties['models']) {
        Write-Host "PDF Scan is already running at $scanUrl"
        Start-Process $scanUrl
    } else {
        Write-Host "Opening $scanUrl. Keep this window open while using PDF Scan."
        Write-Host 'Press Ctrl+C to stop the app.'
        & '.venv/Scripts/python.exe' -m pdfscan serve --port $Port --open-browser
        if ($LASTEXITCODE -ne 0) { throw "PDF Scan could not start (exit code $LASTEXITCODE). See the message above." }
    }
} catch {
    Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
    Read-Host 'Press Enter to close this window'
    exit 1
} finally {
    Pop-Location
}
