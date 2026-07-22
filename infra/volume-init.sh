#!/bin/sh
set -eu

desired_owner="$(id -u rdkwt):$(id -g rdkwt)"
ownership_marker=/state/.rdkwt-ownership-v2
needs_migration=false

for directory in /state /assets /runs /cache; do
  mkdir -p "$directory"
  if [ "$(stat -c '%u:%g' "$directory")" != "$desired_owner" ]; then
    needs_migration=true
  fi
done

if [ ! -f "$ownership_marker" ] \
  || [ "$(cat "$ownership_marker" 2>/dev/null || true)" != "$desired_owner" ]; then
  needs_migration=true
fi

if [ "$needs_migration" = true ]; then
  chown -R rdkwt:rdkwt /state /assets /runs /cache
  printf '%s\n' "$desired_owner" >"$ownership_marker"
  chown rdkwt:rdkwt "$ownership_marker"
fi
