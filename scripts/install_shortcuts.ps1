# Creates the "AJ" on/off shortcut on the Desktop and in the Start menu.
# Clicking it turns AJ on; clicked while he's on, it brings his window to the front and wakes him
# (python -m aj.toggle). Off is the window's Close button, the tray, or "Computer, turn off".
# The shortcut stores absolute paths: run this again after moving the project folder.
# Remove them with: scripts\install_shortcuts.ps1 -Remove
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$targets = @(
  (Join-Path ([Environment]::GetFolderPath("Desktop")) "AJ.lnk"),
  (Join-Path ([Environment]::GetFolderPath("Programs")) "AJ.lnk")
)
if ($Remove) {
  foreach ($t in $targets) { if (Test-Path $t) { Remove-Item $t; Write-Host "removed $t" } }
  return
}
$shell = New-Object -ComObject WScript.Shell
foreach ($t in $targets) {
  $lnk = $shell.CreateShortcut($t)
  $lnk.TargetPath = Join-Path $root ".venv\Scripts\pythonw.exe"
  $lnk.Arguments = "-m aj.toggle"
  $lnk.WorkingDirectory = $root
  $lnk.Description = "Turn AJ on or off"
  $lnk.Save()
  Write-Host "created $t"
}
