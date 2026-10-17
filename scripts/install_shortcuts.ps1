# Creates the "Ultron" on/off shortcut on the Desktop and in the Start menu.
# Clicking it turns Ultron on; clicking it again turns him off (python -m ultron.toggle).
# Remove them with: scripts\install_shortcuts.ps1 -Remove
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$targets = @(
  (Join-Path ([Environment]::GetFolderPath("Desktop")) "Ultron.lnk"),
  (Join-Path ([Environment]::GetFolderPath("Programs")) "Ultron.lnk")
)
if ($Remove) {
  foreach ($t in $targets) { if (Test-Path $t) { Remove-Item $t; Write-Host "removed $t" } }
  return
}
$shell = New-Object -ComObject WScript.Shell
foreach ($t in $targets) {
  $lnk = $shell.CreateShortcut($t)
  $lnk.TargetPath = Join-Path $root ".venv\Scripts\pythonw.exe"
  $lnk.Arguments = "-m ultron.toggle"
  $lnk.WorkingDirectory = $root
  $lnk.Description = "Turn Ultron on or off"
  $lnk.Save()
  Write-Host "created $t"
}
