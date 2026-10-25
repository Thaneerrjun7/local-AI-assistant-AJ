# Creates the "AJ (Speak).lnk" shortcut on the Desktop: click it and a small speech-to-text window
# opens (python -m aj.speak, run with pythonw so no console appears). It is a plain desktop
# shortcut the boss asked for (2026-10-22); nothing here runs at sign-in.
# Remove it with: scripts\install_speak_shortcut.ps1 -Remove
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$link = Join-Path ([Environment]::GetFolderPath("Desktop")) "AJ (Speak).lnk"
if ($Remove) {
  if (Test-Path -LiteralPath $link) { Remove-Item -LiteralPath $link; Write-Host "removed $link" }
  else { Write-Host "no shortcut at $link" }
  return
}
$python = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "no ${python} found: make the project's virtual environment first" }
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($link)
$lnk.TargetPath = $python
$lnk.Arguments = "-m aj.speak"
$lnk.WorkingDirectory = $root
$lnk.Description = "AJ (Speak): say it, and it is typed where your cursor is"
$icon = Join-Path $root "aj\speak\aj_speak.ico"  # made by: python -m aj.speak --make-icon <path>
if (Test-Path -LiteralPath $icon) { $lnk.IconLocation = "$icon,0" }
$lnk.Save()
Write-Host "created $link"
