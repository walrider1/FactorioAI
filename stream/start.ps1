# Starts the watchdog in the background, with no window. It is the only thing that needs to run: it serves the phone
# app, starts and stops the stream (button or daily schedule), and while the stream is live it runs the monitor
# (overlay + screens), the relay, the bot's run and the game client, all hidden. When the stream ends it closes them.
#   powershell -ExecutionPolicy Bypass -File stream\start.ps1      (stop everything: stream\stop.ps1)
# What it does is written to runs\watch\watchdog.log.
$root = Split-Path -Parent $PSScriptRoot
$watch = Join-Path $root "runs\watch"
New-Item -ItemType Directory -Force $watch | Out-Null
if (Get-NetTCPConnection -LocalPort 8740 -State Listen -ErrorAction SilentlyContinue) {
  Write-Host "the watchdog is already running (port 8740). Log: $watch\watchdog.log"
  exit 0
}
Start-Process -FilePath "uv" -ArgumentList "run --no-sync --with obsws-python python stream/watchdog.py" `
  -WorkingDirectory $root -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $watch "watchdog.out") -RedirectStandardError (Join-Path $watch "watchdog.err")
Start-Sleep -Seconds 8
if (Get-NetTCPConnection -LocalPort 8740 -State Listen -ErrorAction SilentlyContinue) {
  Write-Host "watchdog running in the background (phone API on port 8740). Log: $watch\watchdog.log"
} else {
  Write-Host "the watchdog did not start; see $watch\watchdog.err"
}
