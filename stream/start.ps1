# Starts the monitor (overlay + screens) and the watchdog (phone API, stream on/off and schedule, hourly Claude
# review; it runs the multistream relay itself while the stream is live). OBS and the bot's run are started by you.
#   powershell -ExecutionPolicy Bypass -File stream\start.ps1
$root = Split-Path -Parent $PSScriptRoot
$jobs = @(
  @{ title = "monitor";  cmd = "uv run --no-sync arena monitor" },
  @{ title = "watchdog"; cmd = "uv run --no-sync --with obsws-python python stream/watchdog.py" }
)
foreach ($j in $jobs) {
  Start-Process powershell -WorkingDirectory $root -ArgumentList @(
    "-NoExit", "-Command", "`$Host.UI.RawUI.WindowTitle = 'stream: $($j.title)'; $($j.cmd)")
}
Write-Host "started: monitor (8730) and watchdog (phone API 8740). Start the stream from the phone app."
