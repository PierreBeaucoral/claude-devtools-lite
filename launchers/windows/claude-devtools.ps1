<#
  Claude DevTools launcher (Windows).

  Starts the Python server if it isn't already running, then opens the
  dashboard authenticated via the /launch cookie handoff. Prefers an app-mode
  Edge/Chrome window so it looks like a standalone app.

  Every failure ends in a message box that says what went wrong. With
  CDL_NO_OPEN set it prints the login URL (or the error) instead: CI uses that.

  The embedded terminal works on Windows 10 1809+ via ConPTY (no extra
  packages). Verify on this machine with:
      python tools\selftest_windows.py
#>
param([int]$Port = 3456)

$ErrorActionPreference = "SilentlyContinue"
Add-Type -AssemblyName System.Windows.Forms

function Fail($msg) {
    if ($env:CDL_NO_OPEN) { Write-Output $msg }
    else { [System.Windows.Forms.MessageBox]::Show($msg, "Claude DevTools") | Out-Null }
    exit 1
}

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = (Resolve-Path (Join-Path $Here "..\..")).Path
$Server = Join-Path $Repo "server.py"
if (-not (Test-Path $Server)) {
    $Server = Join-Path $env:USERPROFILE "claude-devtools-lite\server.py"
}
if (-not (Test-Path $Server)) { Fail "server.py not found next to this launcher." }

# Ask each candidate for its own python.exe: that resolves the `py` launcher,
# and skips the Microsoft Store stub (it only prints "Python was not found").
$Python = $null
foreach ($c in "python", "python3", "py") {
    if (-not (Get-Command $c)) { continue }
    $exe = & $c -c "import sys; assert sys.version_info >= (3, 9); print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $exe) { $Python = "$exe".Trim(); break }
}
if (-not $Python) {
    Fail "Python 3.9+ not found. Install it from python.org (tick 'Add python.exe to PATH')."
}

# a plain TCP connect: Invoke-WebRequest would go through the system proxy
function Test-Server {
    $c = New-Object Net.Sockets.TcpClient
    try { return $c.ConnectAsync("127.0.0.1", $Port).Wait(500) }
    catch { return $false }
    finally { $c.Close() }
}

if (-not (Test-Server)) {
    # one pre-quoted string: PowerShell 5.1 does not quote array arguments, so
    # a path with a space ("C:\Users\Jean Dupont\...") reached Python split
    Start-Process -FilePath $Python -ArgumentList "`"$Server`" --port $Port" `
                  -WindowStyle Hidden -WorkingDirectory $Repo
    for ($i = 0; $i -lt 60 -and -not (Test-Server); $i++) { Start-Sleep -Milliseconds 250 }
    if (-not (Test-Server)) {
        Fail "The server did not start. To see why, run this in a terminal:`n`n`"$Python`" `"$Server`""
    }
}

# a one-time login URL (60 s): the token itself never reaches the browser's
# command line; the helper also refuses a server that doesn't hold our token.
# Judged by its output, and its error text goes into the message box.
$Out = @(& $Python $Server --port "$Port" --launch-url 2>&1 | ForEach-Object { "$_" })
$Target = @($Out | Where-Object { $_ -like "http*" })[0]
if (-not $Target) {
    Fail ("Could not get a login URL from the server on port $Port.`n`n" + ($Out -join "`n"))
}
if ($env:CDL_NO_OPEN) { Write-Output $Target; exit 0 }

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
