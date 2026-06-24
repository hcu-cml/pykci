# Create a FRESH, EMPTY 3DCityDB v5 PostgreSQL/PostGIS instance, constrained to
# the fair envelope (cpuset 0-7, 20 GB). Recreates container + volume each call.
$ErrorActionPreference = "Continue"   # native docker stderr must not abort the script

# user-defined bridge network (Docker Desktop/Windows has no usable --network host)
if (-not (docker network ls --format '{{.Name}}' | Select-String -Quiet '^cdbnet$')) {
  docker network create cdbnet 2>&1 | Out-Null
}

docker rm -f cdb 2>&1 | Out-Null
docker volume rm cdb_data 2>&1 | Out-Null
docker volume create cdb_data 2>&1 | Out-Null

docker run -d --name cdb --network cdbnet `
  --cpuset-cpus 0-7 --memory 20g --memory-swap 20g `
  -p 5432:5432 `
  -e POSTGRES_PASSWORD=changeMe -e SRID=25832 `
  -v cdb_data:/var/lib/postgresql `
  3dcitydb/3dcitydb-pg | Out-Null

Write-Host ">> waiting for the FINAL postgres server (init scripts done, TCP up) ..."
# The postgres entrypoint runs a TEMP local-socket server for the 3DCityDB init
# scripts, then restarts for the real TCP server. We must wait for the SECOND
# "ready to accept connections" (the final server) or the importer hits a
# connection-refused race over the network.
$ok = $false
for ($i = 0; $i -lt 120; $i++) {
  $logs = (docker logs cdb 2>&1 | Out-String)
  $readyCount = ([regex]::Matches($logs, "ready to accept connections")).Count
  if (($logs -match "PostgreSQL init process complete") -and ($readyCount -ge 2)) { $ok = $true; break }
  Start-Sleep -Seconds 2
}
if (-not $ok) { Write-Host "postgres final server did not come up in time"; docker logs cdb 2>&1 | Select-Object -Last 20; exit 1 }
# Confirm TCP connectivity FROM the cdbnet network (same path the importer uses)
$net = $false
for ($i = 0; $i -lt 30; $i++) {
  docker run --rm --network cdbnet 3dcitydb/3dcitydb-pg pg_isready -h cdb -p 5432 -U postgres 2>&1 | Out-Null
  if ($LASTEXITCODE -eq 0) { $net = $true; break }
  Start-Sleep -Seconds 2
}
if (-not $net) { Write-Host "postgres not reachable over cdbnet TCP"; exit 1 }
docker exec cdb psql -U postgres -d postgres -c "SELECT count(*) AS feature_rows_should_be_0 FROM citydb.feature;" 2>$null
docker inspect cdb --format 'CpuSet={{.HostConfig.CpusetCpus}} Mem={{.HostConfig.Memory}}'
Write-Host ">> 3DCityDB ready and empty."
