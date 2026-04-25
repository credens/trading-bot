#!/bin/bash

# --- Configuración Hardcodeada ---
REMOTE_IP="66.97.46.138"
REMOTE_PORT="5905"
REMOTE_USER="root"
REMOTE_PASS="F3d3R1c0"
API_PORT="8082"

echo "🔪 Saneando conexiones locales..."
pkill -9 -f "ssh.*-L $API_PORT" 2>/dev/null
sleep 1

echo "🔗 Abriendo Túnel SSH (Puerto $API_PORT)..."
# Abre el túnel en background sin ejecutar comandos remotos
sshpass -p "$REMOTE_PASS" ssh -o StrictHostKeyChecking=no -p $REMOTE_PORT -L $API_PORT:localhost:$API_PORT $REMOTE_USER@$REMOTE_IP -N &

echo "✅ Túnel establecido."
echo "💻 Lanzando Dashboard Local..."

cd "/Users/credens/web/polymarket/polymarket-dashboard"
npm run dev -- --port 5173
