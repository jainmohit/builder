#!/bin/bash
set -e
ID="$1"; PORT="$2"
[ -z "$ID" ] || [ -z "$PORT" ] && { echo "usage: new_project.sh <id> <port>"; exit 1; }

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/projects/$ID"

[ -d "$DIR" ] && { echo "Project $ID already exists. Use start_project.sh"; exit 1; }
rsync -a --exclude node_modules "$ROOT/templates/base/" "$DIR/"
cd "$DIR"
git init -q && git add -A && (git commit -qm "initial template" || true)

docker rm -f "proj-$ID" >/dev/null 2>&1 || true
docker run -d --name "proj-$ID" \
  -v "$DIR":/app \
  -v "nm-$ID":/app/node_modules \
  -p "$PORT":5173 \
  --memory 3g --cpus 2 \
  builder-base

echo "Project $ID running at http://localhost:$PORT"