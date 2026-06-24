#!/usr/bin/env bash
# Poll a container's memory every 3s until it exits. Records both docker-stats
# working-set usage and cgroup memory.peak (monotonic high-water incl. cache).
# Usage: mempoll.sh <container_name> <outfile>
name="$1"; out="$2"
echo "# ts | usage | peak_bytes  (container=$name)" > "$out"
# wait up to 180s for the container to appear (runner may start it after us)
for i in $(seq 1 180); do
  docker ps --format '{{.Names}}' | grep -q "^${name}$" && break
  sleep 1
done
while docker ps --format '{{.Names}}' | grep -q "^${name}$"; do
  usage=$(docker stats --no-stream --format '{{.MemUsage}}' "$name" 2>/dev/null)
  peak=$(docker exec "$name" cat /sys/fs/cgroup/memory.peak 2>/dev/null)
  echo "$(date +%s) | $usage | $peak" >> "$out"
done
echo "# container $name exited" >> "$out"
