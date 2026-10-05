#!/bin/sh
if [ ! -d /app/node_modules/.bin ]; then
  mkdir -p /app/node_modules
  cp -a /opt/cache/node_modules/. /app/node_modules/
fi
npm install --no-audit --no-fund --prefer-offline || echo "WARNING: npm install failed, starting anyway"
exec "$@"