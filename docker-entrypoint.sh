#!/bin/sh
# Make sure the data folder belongs to the app user, then drop root.
# Hosts like Unraid often create the mounted folder as root, which would
# otherwise stop the app from writing its database.
set -e
PUID="${PUID:-99}"
PGID="${PGID:-100}"
DATA_DIR="${DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    if [ "$(stat -c %u:%g "$DATA_DIR")" != "$PUID:$PGID" ]; then
        chown -R "$PUID:$PGID" "$DATA_DIR"
    fi
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"
