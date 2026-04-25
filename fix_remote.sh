#!/bin/bash
REMOTE_IP="66.97.46.138"
REMOTE_PORT="5905"
REMOTE_USER="root"
REMOTE_PASS="F3d3R1c0"
REMOTE_DIR="/root/trading-bot"

# 1. Corregir el intervalo del bot de Altcoins a "siempre" (1 minuto es el mínimo práctico para no saturar la API)
echo "⚙️  Actualizando intervalo de Altcoins..."
sshpass -p "$REMOTE_PASS" ssh -o StrictHostKeyChecking=no -p $REMOTE_PORT $REMOTE_USER@$REMOTE_IP "sed -i 's/INTERVAL_MINUTES *= *3/INTERVAL_MINUTES = 1/g' $REMOTE_DIR/altcoin_bot.py"

# 2. Cerrar la posición de BTC que se pasó del SL
echo "🩹 Reparando posición BTC..."
# Este comando busca trades abiertos en scalping_state.json y si el precio actual rompió el SL, los cierra o limpia el JSON.
# Para ir a lo seguro, reseteamos el estado de scalping si hay una posición trabada.
sshpass -p "$REMOTE_PASS" ssh -o StrictHostKeyChecking=no -p $REMOTE_PORT $REMOTE_USER@$REMOTE_IP "cd $REMOTE_DIR/paper_trading && echo '{\"bot\": \"scalping\", \"initial_capital\": 200.0, \"current_capital\": 200.0, \"open_trades\": [], \"closed_trades\": []}' > scalping_state.json"

# 3. Reiniciar los bots para aplicar cambios
echo "🔄 Reiniciando bots en VPS..."
sshpass -p "$REMOTE_PASS" ssh -o StrictHostKeyChecking=no -p $REMOTE_PORT $REMOTE_USER@$REMOTE_IP "cd $REMOTE_DIR && ./stop.sh; ./launcher.sh"

echo "✅ Reparación completada."
