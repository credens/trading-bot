#!/bin/bash
# ─── Trading Bot HQ — VPS Deploy Script ─────────────────────────────────────
# Ejecutar UNA vez en el VPS para configurar todo.
# Uso: scp deploy.sh user@vps:~ && ssh user@vps 'bash deploy.sh'
#
# Prerequisitos: Ubuntu/Debian con acceso root o sudo

set -e

echo "🤖 Trading Bot HQ — Setup VPS"
echo "════════════════════════════════"

# ─── 1. Actualizar sistema e instalar dependencias ──────────────────────────
echo ""
echo "📦 Instalando dependencias del sistema..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip python3-venv git curl nodejs npm

# ─── 2. Crear directorio del proyecto ───────────────────────────────────────
BOT_DIR="$HOME/trading-bot-hq"
if [ ! -d "$BOT_DIR" ]; then
    echo "📁 Creando $BOT_DIR..."
    mkdir -p "$BOT_DIR"
fi

echo ""
echo "⚠️  Copiá los archivos del proyecto a $BOT_DIR antes de continuar."
echo "    Desde tu Mac:"
echo "    scp -r /Users/credens/web/polymarket/*.py user@vps:$BOT_DIR/"
echo "    scp -r /Users/credens/web/polymarket/*.sh user@vps:$BOT_DIR/"
echo "    scp /Users/credens/web/polymarket/.env user@vps:$BOT_DIR/"
echo "    scp -r /Users/credens/web/polymarket/polymarket-dashboard user@vps:$BOT_DIR/"
echo ""
read -p "¿Ya copiaste los archivos? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Copiá los archivos y volvé a ejecutar este script."
    exit 1
fi

cd "$BOT_DIR"

# ─── 3. Instalar dependencias Python ────────────────────────────────────────
echo ""
echo "🐍 Instalando dependencias Python..."
pip3 install python-binance python-dotenv pandas numpy requests --break-system-packages 2>/dev/null || \
pip3 install python-binance python-dotenv pandas numpy requests

# ─── 4. Setup Dashboard (Node.js) ──────────────────────────────────────────
echo ""
echo "📊 Instalando dashboard..."
if [ -d "$BOT_DIR/polymarket-dashboard" ]; then
    cd "$BOT_DIR/polymarket-dashboard"
    npm install --silent
    cd "$BOT_DIR"
    echo "✓ Dashboard instalado"
else
    echo "⚠️ No se encontró polymarket-dashboard/ — saltando"
fi

# ─── 5. Crear directorios de estado ─────────────────────────────────────────
mkdir -p "$BOT_DIR/paper_trading"
mkdir -p "$BOT_DIR/altcoin_data"

# ─── 6. Hacer scripts ejecutables ───────────────────────────────────────────
chmod +x "$BOT_DIR/launcher.sh" "$BOT_DIR/stop.sh" 2>/dev/null

# ─── 7. Adaptar launcher.sh para Linux ─────────────────────────────────────
# Reemplazar comandos macOS con equivalentes Linux
echo ""
echo "🔧 Adaptando scripts para Linux..."
sed -i 's|/opt/homebrew/bin/python3.14|python3|g' "$BOT_DIR/launcher.sh"
sed -i 's|osascript.*||g' "$BOT_DIR/launcher.sh"
sed -i 's|open "http://localhost:5173"||g' "$BOT_DIR/launcher.sh"
sed -i 's|osascript.*||g' "$BOT_DIR/stop.sh"

# ─── 8. Crear servicio systemd ─────────────────────────────────────────────
echo ""
echo "⚙️  Creando servicio systemd..."

sudo tee /etc/systemd/system/trading-bots.service > /dev/null << EOF
[Unit]
Description=Trading Bot HQ
After=network.target

[Service]
Type=forking
User=$USER
WorkingDirectory=$BOT_DIR
ExecStart=/bin/bash $BOT_DIR/launcher.sh
ExecStop=/bin/bash $BOT_DIR/stop.sh
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable trading-bots.service
echo "✓ Servicio trading-bots creado y habilitado"

# ─── 9. Instalar Cloudflare Tunnel ─────────────────────────────────────────
echo ""
echo "☁️  Instalando cloudflared..."
if ! command -v cloudflared &> /dev/null; then
    curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
    sudo dpkg -i cloudflared.deb
    rm cloudflared.deb
    echo "✓ cloudflared instalado"
else
    echo "✓ cloudflared ya instalado"
fi

echo ""
echo "═══════════════════════════════════════════════════════"
echo "✅ Setup completo!"
echo ""
echo "Pasos siguientes:"
echo ""
echo "1. Configurar Cloudflare Tunnel:"
echo "   cloudflared tunnel login"
echo "   cloudflared tunnel create trading-dashboard"
echo "   # Anotar el tunnel ID"
echo ""
echo "2. Crear config:"
echo "   mkdir -p ~/.cloudflared"
echo "   cat > ~/.cloudflared/config.yml << 'YAML'"
echo "   tunnel: <TUNNEL_ID>"
echo "   credentials-file: ~/.cloudflared/<TUNNEL_ID>.json"
echo "   ingress:"
echo "     - service: http://localhost:5173"
echo "   YAML"
echo ""
echo "3. Crear servicio del tunnel:"
echo "   sudo cloudflared service install"
echo "   sudo systemctl enable cloudflared"
echo "   sudo systemctl start cloudflared"
echo ""
echo "4. En Cloudflare Zero Trust dashboard:"
echo "   - Ir a Access > Applications"
echo "   - Agregar app: self-hosted, URL del tunnel"
echo "   - Policy: Allow, email = federico.batistta@gmail.com"
echo ""
echo "5. Iniciar bots:"
echo "   sudo systemctl start trading-bots"
echo "   journalctl -u trading-bots -f"
echo ""
echo "═══════════════════════════════════════════════════════"
