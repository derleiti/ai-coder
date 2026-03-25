#!/data/data/com.termux/files/usr/bin/bash
# ai-coder Termux Installer
# curl -sL https://ailinux.me/ai-coder-termux | bash

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'

echo -e "${CYAN}"
echo "╔══════════════════════════════════════╗"
echo "║   ai-coder — Termux Installer        ║"
echo "║   AILinux Agent für Android          ║"
echo "╚══════════════════════════════════════╝"
echo -e "${NC}"

# Deps installieren
echo -e "${CYAN}[1/4] Pakete installieren...${NC}"
pkg update -y -q
pkg install -y python git curl openssl-tool 2>/dev/null | grep -E "install|upgrade" || true

# ai-coder deps (kein pip upgrade — kaputt in Termux)
echo -e "${CYAN}[2/4] Python-Deps installieren...${NC}"
pip3 install --quiet httpx rich typer certifi 2>/dev/null || \
  pip install --quiet httpx rich typer certifi

echo -e "${CYAN}[3/4] ai-coder holen...${NC}" 

# Source von GitHub holen
INSTALL_DIR="$HOME/.local/lib/aicoder-src"
rm -rf "$INSTALL_DIR"
git clone --depth=1 -q https://github.com/derleiti/ai-coder.git "$INSTALL_DIR"

# Wrapper-Script
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/aicoder" << 'WRAPPER'
#!/data/data/com.termux/files/usr/bin/bash
export PYTHONPATH="$HOME/.local/lib/aicoder-src:$PYTHONPATH"
exec python3 -m aicoder "$@"
WRAPPER
chmod +x "$HOME/.local/bin/aicoder"

# PATH sicherstellen
if ! grep -q 'local/bin' "$HOME/.bashrc" 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
fi

echo -e "${CYAN}[4/4] Fertig!${NC}"
echo ""
echo -e "${GREEN}✓ ai-coder installiert!${NC}"
echo ""
echo "  Starten:"
echo "    source ~/.bashrc"
echo "    aicoder"
echo ""
echo "  Oder direkt:"
echo "    $HOME/.local/bin/aicoder"
echo ""
echo -e "${CYAN}  Backend: https://api.ailinux.me${NC}"
echo -e "${CYAN}  Beta-Code: AILINUX2026${NC}"
