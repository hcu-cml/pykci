#!/usr/bin/env bash
# Poll TWO containers' working-set memory while c2 (the short-lived importer)
# is alive. c1 = long-lived (postgres), c2 = importer (marks import duration).
# Usage: mempoll2.sh <c1> <c2> <outfile>
c1="$1"; c2="$2"; out="$3"
echo "# ts | ${c1}_usage | ${c2}_usage" > "$out"
for i in $(seq 1 180); do
  docker ps --format '{{.Names}}' | grep -q "^${c2}$" && break
  sleep 1
done
while docker ps --format '{{.Names}}' | grep -q "^${c2}$"; do
  u1=$(docker stats --no-stream --format '{{.MemUsage}}' "$c1" 2>/dev/null)
  u2=$(docker stats --no-stream --format '{{.MemUsage}}' "$c2" 2>/dev/null)
  echo "$(date +%s) | $u1 | $u2" >> "$out"
done
echo "# importer $c2 exited" >> "$out"
