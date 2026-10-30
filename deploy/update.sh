#!/bin/bash
# Mise à jour automatique : git pull, et redémarrage seulement s'il y a du nouveau.
set -e
cd /opt/nhl-mcp
git fetch -q origin main
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
    git reset -q --hard origin/main
    ./venv/bin/pip install -q -r requirements.txt
    systemctl restart nhl-mcp
    echo "nhl-mcp mis à jour vers $(git rev-parse --short HEAD)"
fi
