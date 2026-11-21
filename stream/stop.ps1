# Stops the stream tooling: the watchdog, the monitor, the relay and their ffmpeg, and any leftover windows from
# earlier versions of start.ps1. It does not touch OBS, the game client, or a bot run that is still playing
# (end the stream from the phone app first: that saves the run and closes the game).
#   powershell -ExecutionPolicy Bypass -File stream\stop.ps1
$patterns = @('stream.watchdog\.py', 'arena(\.exe)?"? monitor', 'main\(\[.monitor.\]\)',
              'stream.relay\.py', 'ffmpeg.*127\.0\.0\.1:1935', "WindowTitle = 'stream: ")
$procs = Get-CimInstance Win32_Process | Where-Object {
  $cmd = $_.CommandLine; $cmd -and ($patterns | Where-Object { $cmd -match $_ })
}
foreach ($p in $procs) {
  & taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null
  Write-Host "stopped $($p.Name) $($p.ProcessId)"
}
if (-not $procs) { Write-Host "nothing was running" }
