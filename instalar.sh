#!/bin/bash

# Crea los .app de Mac para iniciar y detener el bot
# Corré este script UNA SOLA VEZ desde Terminal para instalar

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Creando aplicaciones Mac..."

# ─── Crear START.app ──────────────────────────────────────────────────────────
START_APP="$SCRIPT_DIR/▶ Iniciar Bot.app"
mkdir -p "$START_APP/Contents/MacOS"

cat > "$START_APP/Contents/MacOS/launcher" << APPLESCRIPT
#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")/../../../" && pwd)"
open -a Terminal "$SCRIPT_DIR/launcher.sh"
APPLESCRIPT

cat > "$START_APP/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>launcher</string>
    <key>CFBundleName</key>
    <string>Iniciar Bot</string>
    <key>CFBundleIdentifier</key>
    <string>com.polymarket.bot.start</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
</dict>
</plist>
PLIST

chmod +x "$START_APP/Contents/MacOS/launcher"

# ─── Crear STOP.app ───────────────────────────────────────────────────────────
STOP_APP="$SCRIPT_DIR/⏹ Detener Bot.app"
mkdir -p "$STOP_APP/Contents/MacOS"

cat > "$STOP_APP/Contents/MacOS/stopper" << APPLESCRIPT
#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")/../../../" && pwd)"
open -a Terminal "$SCRIPT_DIR/stop.sh"
APPLESCRIPT

cat > "$STOP_APP/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>stopper</string>
    <key>CFBundleName</key>
    <string>Detener Bot</string>
    <key>CFBundleIdentifier</key>
    <string>com.polymarket.bot.stop</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
</dict>
</plist>
PLIST

chmod +x "$STOP_APP/Contents/MacOS/stopper"

# Hacer ejecutables los scripts
chmod +x "$SCRIPT_DIR/launcher.sh"
chmod +x "$SCRIPT_DIR/stop.sh"

echo ""
echo "✅ Listo! Se crearon dos apps en tu carpeta:"
echo "   ▶ Iniciar Bot.app  → arranca el bot + dashboard"
echo "   ⏹ Detener Bot.app  → detiene todo"
echo ""
echo "Si Mac te dice 'no se puede abrir porque es de un desarrollador no identificado':"
echo "   → Click derecho → Abrir → Abrir de todas formas"
