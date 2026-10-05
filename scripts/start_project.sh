#!/bin/bash
ID="$1"; PORT="$2"
[ -z "$ID" ] || [ -z "$PORT" ] && { echo "usage: start_project.sh <id> <port>"; exit 1; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

docker rm -f "proj-$ID" >/dev/null 2>&1
docker run -d --name "proj-$ID" \
  -v "$ROOT/projects/$ID":/app \
  -v "nm-$ID":/app/node_modules \
  -p "$PORT":5173 --memory 3g --cpus 2 \
  builder-base >/dev/null || exit 1

for i in $(seq 1 60); do
  if curl -fs -o /dev/null "http://localhost:$PORT"; then
    echo "Ready: http://localhost:$PORT"; exit 0
  fi
  [ "$(docker inspect -f '{{.State.Running}}' "proj-$ID")" != "true" ] && break
  sleep 1
done
echo "Project did not come up. Last logs:"
docker logs --tail 30 "proj-$ID"
exit 1