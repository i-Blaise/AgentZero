# Deploying AgentZero to an Ubuntu VPS (Apache + systemd)

Target layout:
- Code:    `/var/www/production/AgentZero`
- Runtime: uvicorn on `127.0.0.1:8080`, managed by systemd
- Public:  Apache reverse proxy on `443` (HTTPS) → uvicorn
- DB:      MongoDB Atlas (external — nothing to install on the VPS)

Replace `agent.example.com` with your real domain throughout.

---

## 0. DNS (do this first — TLS needs it to propagate)

Create an **A record** for `agent.example.com` pointing to your VPS's public IP.
Verify from your laptop: `dig +short agent.example.com` should return the VPS IP.

## 1. Whitelist the VPS in MongoDB Atlas

Atlas → your cluster → **Network Access → Add IP Address** → enter the VPS public IP.
Without this, the app can connect from your Mac but not from the server.

## 2. System packages (on the VPS)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip apache2 \
                    certbot python3-certbot-apache git
sudo a2enmod proxy proxy_http ssl headers
```

## 3. Get the code onto the server

```bash
sudo mkdir -p /var/www/production
# Option A — git (recommended):
sudo git clone <your-repo-url> /var/www/production/AgentZero
# Option B — from your Mac, if no remote yet:
#   rsync -av --exclude venv --exclude .git \
#     ~/Documents/Projects/Personal/AgentZero/ \
#     user@VPS:/tmp/AgentZero/ && sudo mv /tmp/AgentZero /var/www/production/
sudo chown -R www-data:www-data /var/www/production/AgentZero
```

## 4. Python environment

```bash
cd /var/www/production/AgentZero
sudo -u www-data python3 -m venv venv
sudo -u www-data venv/bin/pip install --upgrade pip
sudo -u www-data venv/bin/pip install -r requirements.txt
```

## 5. Create the production `.env`

```bash
sudo -u www-data nano /var/www/production/AgentZero/.env
```

Paste (fill in your real values):

```
TELEGRAM_BOT_TOKEN=8740059893:AAGS2Sr16qFiBx5vKgHlWKqC17nF6lsu_pg
ALLOWED_CHAT_ID=572112750
TELEGRAM_MODE=webhook
WEBHOOK_URL=https://agent.example.com
WEBHOOK_SECRET=<run: openssl rand -hex 32>

MONGODB_URI=mongodb+srv://agent-user:agent-pass@agents.2mj9rd6.mongodb.net/?retryWrites=true&w=majority
MONGODB_DB=agentzero

LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_CHAT_MODEL=gpt-4o
OPENAI_DIGEST_MODEL=gpt-4o

TIMEZONE=Africa/Accra
AUTONOMY_ENABLED=true
HEARTBEAT_MINUTES=30
QUIET_HOURS_START=21
QUIET_HOURS_END=8
NUDGE_COOLDOWN_HOURS=4
```

Lock it down:

```bash
sudo chmod 600 /var/www/production/AgentZero/.env
sudo chown www-data:www-data /var/www/production/AgentZero/.env
```

## 6. Install the systemd service

```bash
sudo cp deploy/agentzero.service /etc/systemd/system/agentzero.service
sudo systemctl daemon-reload
sudo systemctl enable --now agentzero
sudo systemctl status agentzero          # should be "active (running)"
sudo journalctl -u agentzero -f          # live logs
```

At this point uvicorn is up on 127.0.0.1:8080 but not yet public. The app will
try to register the webhook on startup; it'll succeed once Apache+TLS are live
(step 8). Confirm it's listening locally:

```bash
curl -s http://127.0.0.1:8080/health     # → {"status":"ok"}
```

## 7. Apache vhost

```bash
sudo cp deploy/agentzero-apache.conf /etc/apache2/sites-available/agentzero.conf
sudo sed -i 's/agent.example.com/YOUR_REAL_DOMAIN/g' \
     /etc/apache2/sites-available/agentzero.conf
sudo a2ensite agentzero
sudo systemctl reload apache2
```

## 8. TLS with Let's Encrypt

```bash
sudo certbot --apache -d agent.example.com
```

Certbot obtains the cert, writes the `:443` vhost, and sets up auto-renewal.
Choose "redirect HTTP→HTTPS" when prompted.

## 9. Register the webhook

The app calls `setWebhook` automatically on startup, but it ran before TLS
existed. Restart it now so it registers against the live HTTPS endpoint:

```bash
sudo systemctl restart agentzero
```

Verify Telegram accepted it:

```bash
curl -s "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo"
```

`"url"` should be `https://agent.example.com/webhook` and `pending_update_count`
low. If `last_error_message` is set, it tells you what Telegram couldn't reach.

## 10. Smoke test

- Message the bot on Telegram → you should get a reply.
- Send `/checkin` → proactive review on demand.
- "remind me to test this in 2 minutes" → ping arrives in 2 min (proves the
  scheduler + reminders work under systemd).

---

## Updating after a code change

```bash
cd /var/www/production/AgentZero
sudo git pull                                   # or rsync again
sudo -u www-data venv/bin/pip install -r requirements.txt   # if deps changed
sudo systemctl restart agentzero
```

## Troubleshooting

| Symptom | Check |
|---|---|
| Service won't start | `sudo journalctl -u agentzero -n 50` |
| DB connection error | VPS IP whitelisted in Atlas? (step 1) |
| Webhook not receiving | `getWebhookInfo` `last_error_message`; Apache `proxy` modules enabled? |
| 403 on /webhook | `WEBHOOK_SECRET` in `.env` matches what the app registered (restart app) |
| No proactive pings | Quiet hours? Cooldown? Try `/checkin` to force one |
| Reminders never fire | Service must stay running; check `systemctl status agentzero` |
