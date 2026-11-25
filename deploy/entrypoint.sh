#!/bin/sh
# Container entrypoint: make DATA_DIR writable, then hand off to the app.
#
# Why this exists. The image runs as the unprivileged `botuser`, which is right:
# a userbot holds a full-access Telegram session and should not run as root. But
# a mounted volume is not owned by `botuser`. On Northflank (Kubernetes) a volume
# arrives root-owned, so `DATA_DIR.mkdir()` in load_env_config() raises
# PermissionError and the container dies before it prints a single log line -
# which reads as "the deploy is broken" rather than "a directory is not writable".
#
# Render never hit this because its free tier has no persistent disk at all.
#
# So: start as root, fix ownership of the one directory the app writes to, then
# drop to `botuser` with `exec` so PID 1 is the bot and SIGTERM reaches it (a
# non-exec'd child would leave the bot unable to shut down cleanly). When the
# platform already runs the image as non-root, the chown is skipped and the app
# starts exactly as before.
set -e

DATA_DIR="${DATA_DIR:-/app/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    # Only touch ownership if it is actually wrong, so a restart does not walk a
    # large volume every time.
    if [ "$(stat -c '%U' "$DATA_DIR" 2>/dev/null)" != "botuser" ]; then
        chown -R botuser:botuser "$DATA_DIR"
    fi
    exec setpriv --reuid=botuser --regid=botuser --init-groups "$@"
fi

# Non-root: the platform already handled permissions (a user:group pin, fsGroup,
# or a writable volume). Verify rather than assume, and fail with a message that
# names the fix - a PermissionError from deep inside the app reads as a broken
# deploy, and the owner has no reason to guess it is a volume ownership problem.
if ! mkdir -p "$DATA_DIR" 2>/dev/null; then
    echo "entrypoint: cannot create $DATA_DIR as uid $(id -u)." >&2
    echo "entrypoint: mount a writable volume there, or set the volume owner." >&2
    exit 1
fi
if [ ! -w "$DATA_DIR" ]; then
    echo "entrypoint: $DATA_DIR exists but is not writable by uid $(id -u)." >&2
    echo "entrypoint: chown the volume to this user, or run the container as root" >&2
    echo "entrypoint: and let this script do it (unset the run-as-user override)." >&2
    exit 1
fi

exec "$@"
