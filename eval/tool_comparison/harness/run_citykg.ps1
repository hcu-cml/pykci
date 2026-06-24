param([string]$Label = "run1")
# Timed 3DCityKG mapping-only run (case=0). Fresh empty embedded Neo4j per run
# (fresh named volume). Pinned to cpuset 0-7, 20 GB. Wall-clocks `gradle run`,
# which exits when the in-process embedded-Neo4j mapping completes.
$ErrorActionPreference = "Continue"   # native docker stderr must not abort the script
$conf  = "d:\citykg_bench\run.conf"
$inDir = "d:\PROJEKTE\llm-kg\input"
$vol   = "citykg_out"
$sp    = "C:\Users\SONNGU~1\AppData\Local\Temp\claude\d--PROJEKTE-llm-kg\57abbee9-347c-44d9-946a-927a0b1329a7\scratchpad"
$log   = Join-Path $sp "citykg_$Label.log"
$tim   = Join-Path $sp "timings.txt"

# Fresh, EMPTY embedded DB
docker rm -f citykg_map 2>&1 | Out-Null
docker volume rm $vol 2>&1 | Out-Null
docker volume create $vol 2>&1 | Out-Null

$sw = [System.Diagnostics.Stopwatch]::StartNew()
docker run --rm --name citykg_map `
  --cpuset-cpus 0-7 --memory 20g --memory-swap 20g `
  -v "${conf}:/home/gradle/src/3dcitykg/config/run.conf:ro" `
  -v "${inDir}:/home/gradle/src/3dcitykg/input:ro" `
  -v "${vol}:/home/gradle/src/3dcitykg/output" `
  tumgis/3dcitykg:latest sh -c "gradle run --no-daemon" *> $log
$rc = $LASTEXITCODE
$sw.Stop()
$secs = [math]::Round($sw.Elapsed.TotalSeconds, 2)
$line = "3dcitykg|$Label|elapsed_s=$secs|cpuset=0-7|mem=20g|exit=$rc"
Add-Content -Path $tim -Value $line
Write-Host $line
Write-Host "--- tail log ---"
Get-Content $log -Tail 35
