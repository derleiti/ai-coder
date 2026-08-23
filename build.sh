#!/bin/bash
# build.sh — ai-coder Release-Build
# Erzeugt: dist/aicoder (Binary), packaging/aicoder_*.deb
set -euo pipefail

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT_DIR"
export PIP_CACHE_DIR="$ROOT_DIR/build/pip-cache"
export PYINSTALLER_CONFIG_DIR="$ROOT_DIR/build/pyinstaller-cache"

VERSION=$(python3 -c "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); print(d['project']['version'])")
ARCH=$(uname -m)
DEB_TEMPLATE_VERSION=$(awk '/^Version:/ {print $2; exit}' packaging/debian/aicoder/DEBIAN/control)
AUR_TEMPLATE_VERSION=$(sed -n 's/^pkgver=//p' packaging/aur/PKGBUILD | head -n1)
if [ "$DEB_TEMPLATE_VERSION" != "$VERSION" ] || [ "$AUR_TEMPLATE_VERSION" != "$VERSION" ]; then
    echo "ERROR: package template version mismatch (project=$VERSION deb=$DEB_TEMPLATE_VERSION aur=$AUR_TEMPLATE_VERSION)" >&2
    exit 1
fi
echo "Building aicoder v${VERSION} (${ARCH})..."

# Build-Umgebung reproduzierbar aus den aktuellen Projekt-Metadaten befüllen.
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install -q pyinstaller setuptools wheel
# Editable setuptools installs can leave an ignored root-level egg-info behind.
# Remove it before reinstalling so PyInstaller copy_metadata() cannot bundle a
# stale project version from a previous build.
rm -rf aicoder.egg-info
.venv/bin/python -m pip install -q --no-build-isolation -e .
METADATA_VERSION=$(.venv/bin/python -c "import importlib.metadata as m; print(m.version('aicoder'))")
if [ "$METADATA_VERSION" != "$VERSION" ]; then
    echo "ERROR: installed metadata version $METADATA_VERSION != project version $VERSION" >&2
    exit 1
fi
.venv/bin/python -m PyInstaller aicoder.spec \
    --distpath dist/ --workpath build/ --noconfirm --clean

echo "Binary: $(ls -lh dist/aicoder | awk '{print $5, $9}')"

# CLI-Einstiegspunkte aus dem kompilierten Binary prüfen.
./dist/aicoder --help >/dev/null
./dist/aicoder agent --help >/dev/null
BINARY_VERSION=$(./dist/aicoder --version | awk '{print $2}')
if [ "$BINARY_VERSION" != "$VERSION" ]; then
    echo "ERROR: binary version $BINARY_VERSION != project version $VERSION" >&2
    exit 1
fi
echo "Binary: OK (version ${BINARY_VERSION})"

# Debian package (nur auf Debian/Ubuntu)
if command -v dpkg-deb &>/dev/null; then
    DEB_ARCH=$(dpkg --print-architecture)
    PKGROOT=$(mktemp -d "${TMPDIR:-/tmp}/aicoder-deb.XXXXXX")
    trap 'rm -rf "$PKGROOT"' EXIT

    cp -a packaging/debian/aicoder/. "$PKGROOT/"
    install -Dm755 dist/aicoder "$PKGROOT/usr/bin/aicoder"
    sed "s/@VERSION@/${VERSION}/g" packaging/debian/aicoder/usr/share/man/man1/aicoder.1 \
        | gzip -9n > "$PKGROOT/usr/share/man/man1/aicoder.1.gz"
    rm -f "$PKGROOT/usr/share/man/man1/aicoder.1"

    find "$PKGROOT" -type d -exec chmod 755 {} +
    find "$PKGROOT/usr/share" -type f -exec chmod 644 {} +
    chmod 644 "$PKGROOT/DEBIAN/control"
    chmod 755 "$PKGROOT/DEBIAN/postinst"
    sed -i "s/^Version:.*/Version: ${VERSION}/" "$PKGROOT/DEBIAN/control"
    sed -i "s/^Architecture:.*/Architecture: ${DEB_ARCH}/" "$PKGROOT/DEBIAN/control"
    LIBC_VERSION=$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')
    if [ -n "$LIBC_VERSION" ]; then
        sed -i "s/^Depends: libc6.*/Depends: libc6 (>= ${LIBC_VERSION})/" \
            "$PKGROOT/DEBIAN/control"
    fi
    INSTALLED_SIZE=$(du -s "$PKGROOT/usr" | cut -f1)
    sed -i "/^Architecture:/a Installed-Size: ${INSTALLED_SIZE}" \
        "$PKGROOT/DEBIAN/control"

    DEBFILE="packaging/aicoder_${VERSION}_${DEB_ARCH}.deb"
    dpkg-deb --build --root-owner-group "$PKGROOT" "$DEBFILE"
    SHA=$(sha256sum "$DEBFILE" | cut -d' ' -f1)
    echo "Debian: $DEBFILE (sha256=$SHA)"
fi

# Binary SHA für AUR
BINSHA=$(sha256sum dist/aicoder | cut -d' ' -f1)
echo "Binary SHA256: $BINSHA"
echo ""
echo "Done. Install:"
echo "  sudo cp dist/aicoder /usr/bin/aicoder"
if [ -n "${DEBFILE:-}" ]; then
    echo "  sudo dpkg -i $DEBFILE"
fi
echo "  # AUR: yay -S aicoder  (nach Push zu AUR)"
