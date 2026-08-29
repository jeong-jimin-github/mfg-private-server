$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = "C:\Users\jm\AppData\Local\Programs\Python\Python310\python.exe"
if (-not (Test-Path $py)) {
    $py = "py"
}
Set-Location $here
Write-Host "Starting VFG local server on http://127.0.0.1:8080"
& $py "$here\server.py" @args
