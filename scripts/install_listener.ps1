# Puts AJ's wake listener in Startup and starts it now: while the laptop is plugged in and AJ is
# off, saying "Hey AJ" or "Computer" starts AJ (python -m aj.listener). On battery it keeps
# the microphone closed and does nothing.
# Remove it from Startup and stop it now with: scripts\install_listener.ps1 -Remove
# Add -WhatIf to either to see what it would do without doing anything.
[CmdletBinding(SupportsShouldProcess)]
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$python = Join-Path $root ".venv\Scripts\python.exe"
$link = Join-Path ([Environment]::GetFolderPath("Startup")) "AJ wake listener.lnk"

if ($Remove) {
  if ($PSCmdlet.ShouldProcess("the running wake listener", "stop")) {
    & $python -m aj.listener --stop  # says whether one was running; waits until it has stopped
  }
  if (Test-Path $link) {
    if ($PSCmdlet.ShouldProcess($link, "remove the Startup shortcut")) {
      Remove-Item $link
      Write-Host "removed $link"
    }
  } else {
    Write-Host "no Startup shortcut at $link"
  }
  return
}

if (-not (Test-Path $pythonw)) { throw "no $pythonw : set up the venv first (README, Setup)" }
if ($PSCmdlet.ShouldProcess($link, "create the Startup shortcut")) {
  $shell = New-Object -ComObject WScript.Shell
  $lnk = $shell.CreateShortcut($link)
  $lnk.TargetPath = $pythonw
  $lnk.Arguments = "-m aj.listener"
  $lnk.WorkingDirectory = $root
  $lnk.Description = "AJ wake listener: say Hey AJ or Computer to start AJ (only while plugged in)"
  $lnk.Save()
  Write-Host "created $link"
}
if ($PSCmdlet.ShouldProcess("$pythonw -m aj.listener", "start the wake listener now")) {
  # A second copy exits at once if one is already running (one listener at a time).
  Start-Process -FilePath $pythonw -ArgumentList "-m", "aj.listener" -WorkingDirectory $root
  Write-Host "wake listener started: it listens only while plugged in and AJ is off"
  Write-Host "log: $env:APPDATA\AJ\logs\listener.log"
}
