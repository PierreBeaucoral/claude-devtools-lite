<#
  Creates Start-menu and Desktop shortcuts for Ember (per-user, no admin).
  Run once:  powershell -ExecutionPolicy Bypass -File install.ps1
#>
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Cmd  = Join-Path $Here "Ember.cmd"
if (-not (Test-Path $Cmd)) { Write-Error "Ember.cmd not found next to install.ps1"; exit 1 }

# the app's own icon, committed next to this script; a stock one if it's gone
$Icon = Join-Path $Here "claude-devtools.ico"
if (Test-Path $Icon) { $Icon = "$Icon,0" } else { $Icon = "$env:SystemRoot\System32\SHELL32.dll,13" }

$WShell = New-Object -ComObject WScript.Shell
$targets = @(
  (Join-Path ([Environment]::GetFolderPath("Desktop")) "Ember.lnk"),
  (Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\Ember.lnk")
)
foreach ($t in $targets) {
    $dir = Split-Path -Parent $t
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $sc = $WShell.CreateShortcut($t)
    $sc.TargetPath       = $Cmd
    $sc.WorkingDirectory = $Here
    $sc.Description      = "Inspect Claude Code sessions, tokens, and outputs"
    $sc.IconLocation     = $Icon
    $sc.Save()
    Write-Host "Created $t"
    # Retire only our old shortcut in this same location. Unrelated shortcuts
    # with the old display name remain untouched.
    $old = Join-Path $dir "Claude DevTools.lnk"
    if (Test-Path $old) {
        $prior = $WShell.CreateShortcut($old)
        if ($prior.TargetPath -eq (Join-Path $Here "Claude DevTools.cmd")) {
            Remove-Item -LiteralPath $old
        }
    }
}
Write-Host ""
Write-Host "Done. The embedded terminal uses ConPTY (Windows 10 1809+)."
Write-Host "Verify it here with:  python tools\selftest_windows.py"
