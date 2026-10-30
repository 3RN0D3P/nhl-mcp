# NHL MCP

Serveur MCP léger pour les stats de la LNH (API publique LNH + expected goals MoneyPuck).
Auto-hébergé sur un LXC Debian, exposé via un tunnel Cloudflare, utilisé comme connecteur dans claude.ai.

## Installation (LXC Debian)

```bash
apt install -y git python3-venv
git clone https://github.com/3RN0D3P/nhl-mcp /opt/nhl-mcp
cd /opt/nhl-mcp
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Service

`/etc/systemd/system/nhl-mcp.service` (le secret reste sur le serveur, jamais dans le dépôt) :

```ini
[Unit]
Description=NHL MCP
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/nhl-mcp
Environment=NHL_MCP_SECRET=TON_SECRET
Environment=PORT=8765
ExecStart=/opt/nhl-mcp/venv/bin/python nhl_mcp.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Mise à jour automatique

Toutes les 5 minutes, `deploy/update.sh` fait `git pull` et redémarre le service s'il y a du nouveau.

```bash
chmod +x /opt/nhl-mcp/deploy/update.sh
ln -sf /opt/nhl-mcp/deploy/nhl-mcp-update.service /etc/systemd/system/
ln -sf /opt/nhl-mcp/deploy/nhl-mcp-update.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now nhl-mcp-update.timer
```

Historique des mises à jour : `journalctl -u nhl-mcp-update`

## Connecteur claude.ai

URL : `https://nhl-mcp.TON_DOMAINE/TON_SECRET/mcp`

## Ajouter une stat

Copier un bloc `@mcp.tool` dans `nhl_mcp.py`, changer l'endpoint et les champs.
Les endpoints utiles sont listés à la fin du fichier.
