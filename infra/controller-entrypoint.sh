#!/bin/sh
set -eu

for directory in /state /assets /runs /cache; do
  if [ ! -d "$directory" ] || [ ! -r "$directory" ] || [ ! -w "$directory" ]; then
    echo "RDK WebToolChain volume is not accessible by the rdkwt user: $directory" >&2
    echo "Run the volume-init service before starting the Controller." >&2
    exit 1
  fi
done

if [ -S /var/run/docker.sock ] \
  && { [ ! -r /var/run/docker.sock ] || [ ! -w /var/run/docker.sock ]; }; then
  echo "Docker socket is not accessible; check DOCKER_GID in infra/.env." >&2
  exit 1
fi

exec "$@"
