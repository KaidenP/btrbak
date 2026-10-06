#!/usr/bin/env bash
#
# Build a binary btrbak .deb without requiring debhelper/dh-python.
#
# The package is pure Python, so we stage it by hand and let dpkg-deb create
# the archive.  Run from anywhere; output lands in dist/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="$(python3 - "$ROOT/src/btrbak/__init__.py" <<'PY'
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
if not match:
    print("cannot determine version from __init__.py", file=sys.stderr)
    sys.exit(1)
print(match.group(1))
PY
)"
DEB_VERSION="${VERSION}-1"
PACKAGE="btrbak_${DEB_VERSION}_all.deb"

if ! command -v pandoc >/dev/null 2>&1; then
    echo "pandoc is required to generate the man pages from docs/*.md (install pandoc)" >&2
    exit 1
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
# mktemp creates the staging root 0700; the archive's "./" entry must be a
# normal 0755 directory or dpkg would chmod the real / on install.
chmod 0755 "$STAGE"

# --- filesystem layout -----------------------------------------------------
install -d "$STAGE/usr/bin"
install -d "$STAGE/usr/lib/python3/dist-packages"
install -d "$STAGE/usr/share/man/man1"
install -d "$STAGE/usr/share/man/man5"
install -d "$STAGE/usr/share/bash-completion/completions"
install -d "$STAGE/usr/share/zsh/vendor-completions"
install -d "$STAGE/usr/share/doc/btrbak"
install -d -m 0755 "$STAGE/etc/btrbak/profiles.d"

# --- python package --------------------------------------------------------
cp -a "$ROOT/src/btrbak" "$STAGE/usr/lib/python3/dist-packages/"
find "$STAGE/usr/lib/python3/dist-packages/btrbak" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$STAGE/usr/lib/python3/dist-packages/btrbak" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
find "$STAGE/usr/lib/python3/dist-packages/btrbak" -type f -name '*.py' -exec chmod 0644 {} +

# --- console script --------------------------------------------------------
cat > "$STAGE/usr/bin/btrbak" <<'PY'
#!/usr/bin/python3
import sys

from btrbak.cli import main

if __name__ == "__main__":
    sys.exit(main())
PY
chmod 0755 "$STAGE/usr/bin/btrbak"

# --- manpages (generated from the committed Markdown source) ---------------
# docs/*.md is the single source of truth; the roff here is produced on every
# build so the man pages can never drift from the Markdown documentation.
#
# Pandoc emits its own verbatim-font detection block that makes groff complain
# about unknown 'C'/'V' fonts, so strip that block and map the verbatim fonts
# to bold before writing the page.
render_man() {
    local src=$1 out=$2 footer=$3
    pandoc -s -f markdown -t man -M "footer=${footer}" "$src" \
        | python3 -c '
import sys

lines = sys.stdin.read().splitlines(keepends=True)
out = []
skip = False
for line in lines:
    if line.startswith(".\\\" Define V font"):
        skip = True
        continue
    if skip and line.startswith(".TH"):
        skip = False
    if not skip:
        out.append(line)

text = "".join(out)
for font in ("V", "C", "VI", "VB", "VBI", "CB", "CI", "CBI"):
    text = text.replace("\\f[" + font + "]", "\\f[B]")
sys.stdout.write(text)
' > "$out"
}
render_man "$ROOT/docs/btrbak.1.md" \
    "$STAGE/usr/share/man/man1/btrbak.1" "btrbak ${VERSION}"
render_man "$ROOT/docs/btrbak-profiles.5.md" \
    "$STAGE/usr/share/man/man5/btrbak-profiles.5" "btrbak ${VERSION}"
gzip -9n "$STAGE/usr/share/man/man1/btrbak.1"
gzip -9n "$STAGE/usr/share/man/man5/btrbak-profiles.5"
chmod 0644 "$STAGE/usr/share/man/man1/btrbak.1.gz" "$STAGE/usr/share/man/man5/btrbak-profiles.5.gz"

# --- shell completions -----------------------------------------------------
install -m 0644 "$ROOT/completions/bash/btrbak" "$STAGE/usr/share/bash-completion/completions/btrbak"
install -m 0644 "$ROOT/completions/zsh/_btrbak" "$STAGE/usr/share/zsh/vendor-completions/_btrbak"

# --- documentation ---------------------------------------------------------
install -m 0644 "$ROOT/debian/copyright" "$STAGE/usr/share/doc/btrbak/copyright"
gzip -9n -c "$ROOT/debian/changelog" > "$STAGE/usr/share/doc/btrbak/changelog.Debian.gz"
gzip -9n -c "$ROOT/README.md" > "$STAGE/usr/share/doc/btrbak/README.gz"
chmod 0644 "$STAGE/usr/share/doc/btrbak/changelog.Debian.gz" "$STAGE/usr/share/doc/btrbak/README.gz"

# --- package metadata ------------------------------------------------------
INSTALLED_SIZE="$(du -sk "$STAGE" | cut -f1)"
install -d "$STAGE/DEBIAN"
sed \
    -e "s/@VERSION@/${DEB_VERSION}/g" \
    -e "s/@INSTALLED_SIZE@/${INSTALLED_SIZE}/g" \
    "$ROOT/debian/control" > "$STAGE/DEBIAN/control"
chmod 0644 "$STAGE/DEBIAN/control"

(
    cd "$STAGE"
    find . -type f -not -path './DEBIAN/*' -print0 \
        | LC_ALL=C sort -z \
        | xargs -0 md5sum \
        | sed 's#  \./#  #'
) > "$STAGE/DEBIAN/md5sums"
chmod 0644 "$STAGE/DEBIAN/md5sums"

# --- build -----------------------------------------------------------------
mkdir -p "$ROOT/dist"
dpkg-deb --build --root-owner-group "$STAGE" "$ROOT/dist/$PACKAGE"

echo "built: dist/$PACKAGE"
