<#
  Claude DevTools launcher (Windows).

  Starts the Python server if it isn't already running, then opens the
  dashboard authenticated via the /launch cookie handoff. Prefers an app-mode
  Edge/Chrome window so it looks like a standalone app.

  The embedded terminal works on Windows 10 1809+ via ConPTY (no extra
  packages). Verify on this machine with:
      python tools\selftest_windows.py
#>
param([int]$Port = 3456)

$ErrorActionPreference = "SilentlyContinue"
$Url  = "http://127.0.0.1:$Port"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = (Resolve-Path (Join-Path $Here "..\..")).Path
$Server = Join-Path $Repo "server.py"
if (-not (Test-Path $Server)) {
    $Server = Join-Path $env:USERPROFILE "claude-devtools-lite\server.py"
}
if (-not (Test-Path $Server)) {
    [System.Windows.Forms.MessageBox]::Show("server.py not found next to this launcher.") | Out-Null
    exit 1
}

$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) { $Python = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
if (-not $Python) {
    Write-Host "Python 3 not found. Install it from python.org or the Microsoft Store."
    exit 1
}

function Test-Server {
    try { $null = Invoke-WebRequest -Uri "$Url/" -TimeoutSec 1 -UseBasicParsing; return $true }
    catch { return $false }
}

if (-not (Test-Server)) {
    Start-Process -FilePath $Python -ArgumentList @($Server, "--port", "$Port") `
                  -WindowStyle Hidden -WorkingDirectory $Repo
    for ($i = 0; $i -lt 40; $i++) {
        if (Test-Server) { break }
        Start-Sleep -Milliseconds 250
    }
}

$TokenFile = Join-Path $env:APPDATA "claude-devtools\token"
$Token = (Get-Content $TokenFile -ErrorAction SilentlyContinue | Select-Object -First 1)
$Target = "$Url/launch?k=$Token"
if ($env:CDL_NO_OPEN) { exit 0 }

$Browsers = @(
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
  "${env:ProgramFiles}\Microsoft\Edge\Application\msedge.exe",
  "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
)
foreach ($b in $Browsers) {
    if (Test-Path $b) {
        Start-Process -FilePath $b -ArgumentList "--app=$Target","--window-size=1500,950"
        exit 0
    }
}
Start-Process $Target   # default browser
