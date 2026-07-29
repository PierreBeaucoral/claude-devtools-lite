<#
  Creates Start-menu and Desktop shortcuts for Claude DevTools (per-user, no admin).
  Run once:  powershell -ExecutionPolicy Bypass -File install.ps1
#>
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Cmd  = Join-Path $Here "Claude DevTools.cmd"
if (-not (Test-Path $Cmd)) { Write-Error "Claude DevTools.cmd not found next to install.ps1"; exit 1 }

$WShell = New-Object -ComObject WScript.Shell
$targets = @(
  (Join-Path ([Environment]::GetFolderPath("Desktop")) "Claude DevTools.lnk"),
  (Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\Claude DevTools.lnk")
)
foreach ($t in $targets) {
    $dir = Split-Path -Parent $t
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $sc = $WShell.CreateShortcut($t)
    $sc.TargetPath       = $Cmd
    $sc.WorkingDirectory = $Here
    $sc.Description      = "Inspect Claude Code sessions, tokens, and outputs"
    $sc.IconLocation     = "$env:SystemRoot\System32\SHELL32.dll,13"
    $sc.Save()
    Write-Host "Created $t"
}
Write-Host ""
Write-Host "Done. Note: the embedded terminal pane is macOS/Linux only —"
Write-Host "run 'claude' in Windows Terminal alongside the dashboard."
