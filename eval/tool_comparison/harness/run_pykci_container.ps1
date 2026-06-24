param([string]$Label = "run1")
# Timed CONTAINERIZED pykci ingest. Reads the GML via bind mount and reaches
# Neo4j through a shared network namespace (localhost:7687 -> neo4j container).
# Pinned to cpuset 0-7, 20 GB. Container exits when ingest completes (wall-clockable).
$ErrorActionPreference = "Continue"
$inDir = "d:\PROJEKTE\llm-kg\input"
$sp    = "C:\Users\SONNGU~1\AppData\Local\Temp\claude\d--PROJEKTE-llm-kg\57abbee9-347c-44d9-946a-927a0b1329a7\scratchpad"
$log   = Join-Path $sp "pykci_container_$Label.log"
$tim   = Join-Path $sp "timings.txt"

docker rm -f pykci_ingest 2>&1 | Out-Null
$sw = [System.Diagnostics.Stopwatch]::StartNew()
docker run --rm --name pykci_ingest --network container:neo4j `
  --cpuset-cpus 0-7 --memory 20g --memory-swap 20g `
  -v "${inDir}:/input:ro" `
  pykci-ingest /input/citygml/hamburg_citygml2_lod2_2025.gml *> $log
$rc = $LASTEXITCODE
$sw.Stop()
$secs = [math]::Round($sw.Elapsed.TotalSeconds, 2)
$line = "pykci_container|$Label|elapsed_s=$secs|cpuset=0-7|mem=20g|exit=$rc"
Add-Content -Path $tim -Value $line
Write-Host $line
Write-Host "--- tail log ---"
Get-Content $log -Tail 16
