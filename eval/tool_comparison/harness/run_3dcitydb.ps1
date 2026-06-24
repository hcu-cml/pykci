param([string]$Label = "run1")
# Timed 3DCityDB v5 CityGML import. Assumes setup_3dcitydb.ps1 already produced a
# fresh empty `cdb` instance. Importer container pinned to cpuset 0-7, 20 GB,
# on the same bridge network as postgres.
$ErrorActionPreference = "Continue"   # native docker stderr must not abort the script
$inDir = "d:\PROJEKTE\llm-kg\input\citygml"
$sp    = "C:\Users\SONNGU~1\AppData\Local\Temp\claude\d--PROJEKTE-llm-kg\57abbee9-347c-44d9-946a-927a0b1329a7\scratchpad"
$log   = Join-Path $sp "3dcitydb_$Label.log"
$tim   = Join-Path $sp "timings.txt"

# Remove any stale importer container (defensive against orphans) before timing.
docker rm -f cdb_import 2>&1 | Out-Null
# Default --index-mode=keep (no tuning) for an out-of-the-box comparison.
$sw = [System.Diagnostics.Stopwatch]::StartNew()
docker run -i --rm --name cdb_import --network cdbnet `
  --cpuset-cpus 0-7 --memory 20g --memory-swap 20g `
  -v "${inDir}:/data" `
  3dcitydb/citydb-tool import citygml `
    -H cdb -P 5432 -d postgres -S citydb -u postgres --db-password=changeMe `
    /data/hamburg_citygml2_lod2_2025.gml *> $log
$rc = $LASTEXITCODE
$sw.Stop()
$secs = [math]::Round($sw.Elapsed.TotalSeconds, 2)
$line = "3dcitydb|$Label|elapsed_s=$secs|cpuset=0-7|mem=20g|exit=$rc"
Add-Content -Path $tim -Value $line
Write-Host $line
Write-Host "--- tail log ---"
Get-Content $log -Tail 30
