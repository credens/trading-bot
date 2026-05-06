#!/bin/bash

set -euo pipefail

LABEL="com.credens.polymarket.bot-supervisor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
rm -f "$PLIST"

echo "Desinstalado: $LABEL"
