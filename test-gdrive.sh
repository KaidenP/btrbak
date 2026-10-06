#!/usr/bin/env bash
# test-gdrive.sh — end-to-end smoke test of the `gdrive` remote (personal Google account).
#
# Sequence:
#   1. obtain OAuth credentials (btrbak gdrive authorize)
#   2. build a throwaway loopback btrfs filesystem, seed a subvolume, and
#      snapshot + upload it to Google Drive
#   3. pause so you can confirm the files landed in Google Drive
#   4. tear down every local artifact (loopback image, config); the uploaded
#      copy in Drive and the OAuth token (reused on the next run) are kept
#
# Requires root (btrbak runs as root; mkfs.btrfs/mount need it).
#
# Usage:
#   sudo ./test-gdrive.sh --client-secret ~/client_secret.json
#   sudo ./test-gdrive.sh --client-secret ~/client_secret.json --folder 1AbCdEfGh... --console
#
# The Drive folder (by name or ID) must already exist and be owned by / shared
# with the account you authorize.

set -euo pipefail

# --- configuration (env-overridable) --------------------------------------

# `sudo` strips the user's PATH, so the venv's btrbak is often not on it;
# fall back to the invoking user's ~/.venv and common system locations.
BTRBAK_BIN="${BTRBAK_BIN:-}"
if [[ -z "$BTRBAK_BIN" ]]; then
    # `sudo` strips the user's PATH, so the venv isn't on it; and a stale
    # deb-installed /usr/bin/btrbak would otherwise be found first. Prefer
    # the invoking user's venv (which has the Google client libs installed),
    # then fall back to a system btrbak.
    _home="$(getent passwd "${SUDO_USER:-$USER}" | cut -d: -f6)"
    if [[ -x "$_home/.venv/bin/btrbak" ]]; then
        BTRBAK_BIN="$_home/.venv/bin/btrbak"
    else
        BTRBAK_BIN="$(command -v btrbak || true)"
    fi
fi
CLIENT_SECRET=""                          # OAuth "Desktop app" client JSON (required)
FOLDER="${FOLDER:-btrbak/test}"           # Drive folder ID, or a name / slash path
TOKEN="${TOKEN:-/etc/btrbak/gdrive-token.json}"
WORKDIR="${WORKDIR:-/tmp/btrbak-loop-test}"
IMG_SIZE="${IMG_SIZE:-512M}"
CONSOLE=0

AUTH_FILE=/etc/btrbak/auth.yaml
PROFILE_FILE=/etc/btrbak/profiles.d/looptest.yaml
MOUNT="$WORKDIR/mnt"
AUTH_BACKUP=""
AUTH_WRITTEN=0      # set once we authorize + write auth.yaml
WORKDIR_CREATED=0   # set once we create the loopback workdir

# --- helpers ---------------------------------------------------------------

warn() { printf '!! %s\n' "$*" >&2; }
info() { printf '== %s\n' "$*"; }

usage() {
    cat >&2 <<'EOF'
usage: sudo test-gdrive.sh --client-secret PATH [--folder ID|NAME] [--console]

  --client-secret PATH  OAuth "Desktop app" client JSON from Google Cloud
  --folder ID|NAME      Google Drive folder ID or unique name (default: btrbak-test)
  --console             use the copy/paste OAuth flow instead of a local web server
EOF
    exit 2
}

die() { warn "$*"; exit 1; }

# Locate the python that runs btrbak (a venv bin sits next to its python) so
# the Google client libraries are installed into the same environment.
resolve_python() {
    local bin dir
    bin="$(command -v "$BTRBAK_BIN" 2>/dev/null || echo "$BTRBAK_BIN")"
    bin="$(readlink -f "$bin" 2>/dev/null || echo "$bin")"
    dir="$(dirname "$bin")"
    for cand in "$dir/python" "$dir/python3" python3; do
        command -v "$cand" >/dev/null 2>&1 && { echo "$cand"; return 0; }
    done
    return 1
}

ensure_deps() {
    local py
    py="$(resolve_python)" || die "cannot locate a python for btrbak"
    if "$py" -c 'import google_auth_oauthlib, googleapiclient' >/dev/null 2>&1; then
        return 0
    fi
    info "google client libraries missing; installing into $py"
    if "$py" -m pip install --quiet google-auth-oauthlib google-api-python-client; then
        info "installed google client libraries"
    else
        die "pip install failed; install python3-google-auth-oauthlib and python3-googleapi instead"
    fi
}

# Tear down everything local. Runs on exit (success or failure) so a loop
# device is never left mounted and no credentials linger.
cleanup() {
    set +e
    (( WORKDIR_CREATED || AUTH_WRITTEN )) || return 0
    info "cleaning up local test artifacts"
    if (( WORKDIR_CREATED )); then
        if [[ -n "${MOUNT:-}" ]] && mountpoint -q "$MOUNT" 2>/dev/null; then
            umount "$MOUNT"
        fi
        rm -rf "${WORKDIR:-}"
    fi
    rm -f "$PROFILE_FILE"
    if (( AUTH_WRITTEN )); then
        if [[ -n "${AUTH_BACKUP:-}" ]] && [[ -f "${AUTH_BACKUP:-}" ]]; then
            mv -f "$AUTH_BACKUP" "$AUTH_FILE"
        else
            rm -f "$AUTH_FILE"
        fi
        # Keep $TOKEN so the next run can skip the authorize step.
    else
        [[ -n "${AUTH_BACKUP:-}" ]] && rm -f "$AUTH_BACKUP"
    fi
    info "done (the uploaded copy remains in Google Drive)"
}
trap cleanup EXIT

# --- argument parsing ------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --client-secret) CLIENT_SECRET="${2:?--client-secret requires a value}"; shift 2 ;;
        --folder)        FOLDER="${2:?--folder requires a value}"; shift 2 ;;
        --console)       CONSOLE=1; shift ;;
        -h|--help)       usage ;;
        *)               warn "unknown argument: $1"; usage ;;
    esac
done

[[ $EUID -eq 0 ]] || die "run as root: sudo $0 --client-secret <client.json>"
[[ -n "$BTRBAK_BIN" ]] || die "btrbak not found (no venv and none on PATH)"
[[ -n "$CLIENT_SECRET" ]] || die "--client-secret is required (the OAuth 'Desktop app' client JSON)"
[[ -f "$CLIENT_SECRET" ]] || die "client secret file not found: $CLIENT_SECRET"
command -v mkfs.btrfs >/dev/null || die "mkfs.btrfs not found (install btrfs-progs)"

ensure_deps
umask 077

# 1. authorize Google (skipped if a token already exists) ------------------

if [[ -s "$TOKEN" ]]; then
    info "already authorized (reusing $TOKEN)"
else
    info "authorizing Google (follow the browser flow)"
    AUTH_ARGS=(gdrive authorize --client-secret "$CLIENT_SECRET" --token "$TOKEN")
    (( CONSOLE )) && AUTH_ARGS+=(--console)
    "$BTRBAK_BIN" "${AUTH_ARGS[@]}"
fi

# 2. wire the credentials into auth.yaml (backing up any existing file) ----

install -d -m 700 /etc/btrbak/profiles.d
if [[ -e "$AUTH_FILE" ]]; then
    AUTH_BACKUP="$(mktemp)"
    cp -a "$AUTH_FILE" "$AUTH_BACKUP"
fi
printf 'gdrive: %s\n' "$TOKEN" > "$AUTH_FILE"
AUTH_WRITTEN=1

# 3. loopback btrfs filesystem ----------------------------------------------

info "building loopback btrfs filesystem ($IMG_SIZE)"
rm -rf "$WORKDIR"; install -d "$WORKDIR"
WORKDIR_CREATED=1
truncate -s "$IMG_SIZE" "$WORKDIR/disk.img"
mkfs.btrfs -q -f "$WORKDIR/disk.img"
install -d "$MOUNT"
mount -o loop "$WORKDIR/disk.img" "$MOUNT"
btrfs subvolume create "$MOUNT/data" >/dev/null
dd if=/dev/urandom of="$MOUNT/data/random.bin" bs=1M count=8 status=none
printf 'hello btrbak\n' > "$MOUNT/data/hello.txt"

# 4. profile config ---------------------------------------------------------

cat > "$PROFILE_FILE" <<EOF
src: $MOUNT/data
dest: $MOUNT/snapshots
tmpdir: $WORKDIR/tmp

profiles:
  daily:
    freq:
      full: 1w
      incr: -1
    keep: 30d
    remotes:
      - type: gdrive
        folder: "$FOLDER"
        auth: gdrive
EOF

# 5. snapshot + upload ------------------------------------------------------

info "running backup -> Google Drive (folder: $FOLDER)"
"$BTRBAK_BIN" run looptest daily --force --force-config -v

echo
info "local manifest:"
"$BTRBAK_BIN" list looptest daily

# 6. pause for manual verification, then cleanup ----------------------------

echo
read -rp "Check Google Drive now. Press Enter to clean up local files (the Drive copy is kept)... "
