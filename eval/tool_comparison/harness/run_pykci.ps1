param([string]$Label = "run1")
# Timed pykci ingest, pinned to logical cores 0-7 (mask 0xFF).
# The venv python.exe spawns a CHILD python.exe that does the real work, so we
# pin EVERY python.exe whose command line contains 'ingest_citygml' on each
# sampling tick (idempotent) and track the peak summed working set across them.
# Assumes neo4j already reset+constrained(cpuset 0-7, 20g)+empty.
$ErrorActionPreference = "Stop"
$proj = "d:\PROJEKTE\llm-kg"
$py   = "d:\PROJEKTE\llm-kg\.venv\Scripts\python.exe"
$sp   = "C:\Users\SONNGU~1\AppData\Local\Temp\claude\d--PROJEKTE-llm-kg\57abbee9-347c-44d9-946a-927a0b1329a7\scratchpad"
$gml  = "input\citygml\hamburg_citygml2_lod2_2025.gml"
$log  = Join-Path $sp "pykci_$Label.log"
$err  = Join-Path $sp "pykci_$Label.err"
$tim  = Join-Path $sp "timings.txt"
$mask = [IntPtr]0xFF

Set-Location $proj
$p = Start-Process -FilePath $py -ArgumentList @("ingest_citygml.py", $gml) `
       -PassThru -NoNewWindow -RedirectStandardOutput $log -RedirectStandardError $err
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$max = 0L
$sawAll = $false
while (-not $p.HasExited) {
  $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
           Where-Object { $_.CommandLine -like '*ingest_citygml*' }
  $sum = 0L; $allPinned = $true
  foreach ($cp in $procs) {
    try {
      $pp = Get-Process -Id $cp.ProcessId -ErrorAction Stop
      if ([int64]$pp.ProcessorAffinity -ne 255) { $pp.ProcessorAffinity = $mask; $allPinned = $false }
      $pp.Refresh(); $sum += $pp.WorkingSet64
    } catch {}
  }
  if ($allPinned -and $procs) { $sawAll = $true }
  if ($sum -gt $max) { $max = $sum }
  Start-Sleep -Milliseconds 1500
}
$sw.Stop()
$p.Refresh()
$peakGB = [math]::Round($max / 1GB, 2)
$secs   = [math]::Round($sw.Elapsed.TotalSeconds, 2)
$line = "pykci|$Label|elapsed_s=$secs|peak_ws_gb=$peakGB|pinned_0-7=$sawAll|exit=$($p.ExitCode)"
Add-Content -Path $tim -Value $line
Write-Host $line
Write-Host "--- tail log ---"
Get-Content $log -Tail 18
