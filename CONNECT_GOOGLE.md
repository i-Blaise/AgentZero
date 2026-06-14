# Connecting Gmail + Google Calendar (read-only)

AgentZero talks to Google through a separate MCP server (`workspace-mcp`) running
on the VPS. AgentZero connects to it over localhost HTTP and exposes its tools to
the LLM. Read-only is enforced by the server's `--read-only` flag.

There are three stages: Google Cloud setup, run the MCP server, point AgentZero at it.

---

## Stage 1 — Google Cloud (you, in a browser)

1. <https://console.cloud.google.com> → create or select a project.
2. **APIs & Services → Enable APIs:** enable **Gmail API** and **Google Calendar API**.
3. **OAuth consent screen:** User type **External**. Fill the app name/email.
   Under **Test users**, add your own Google address (test users can use the app
   without Google verifying it). Add the read-only scopes:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/calendar.readonly`
4. **Credentials → Create credentials → OAuth client ID → Web application.**
   - Add an **Authorized redirect URI**: `http://localhost:8001/oauth2callback`
   - Save the **Client ID** and **Client secret**.

## Stage 2 — Run the MCP server on the VPS

```bash
# Install into its own venv
sudo mkdir -p /opt/workspace-mcp
sudo python3 -m venv /opt/workspace-mcp/venv
sudo /opt/workspace-mcp/venv/bin/pip install workspace-mcp

# Config + credentials
sudo nano /opt/workspace-mcp/.env
```

`/opt/workspace-mcp/.env`:
```
GOOGLE_OAUTH_CLIENT_ID=your-client-id
GOOGLE_OAUTH_CLIENT_SECRET=your-client-secret
WORKSPACE_MCP_PORT=8001
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8001/oauth2callback
OAUTHLIB_INSECURE_TRANSPORT=1
```
(Leave `MCP_ENABLE_OAUTH21` unset — this is single-user; the MCP transport stays
unauthenticated on localhost, and only Google's OAuth is involved.)

```bash
sudo chown -R www-data:www-data /opt/workspace-mcp
sudo chmod 600 /opt/workspace-mcp/.env
```

### One-time Google consent (headless)

The server needs you to approve access once in a browser. From your laptop, open
an SSH tunnel so the OAuth redirect can reach the VPS:

```bash
ssh -L 8001:localhost:8001 root@YOUR_VPS_IP
```

Keep that open. On the VPS (in another shell), start the server manually once:
```bash
sudo -u www-data bash -c 'set -a; . /opt/workspace-mcp/.env; set +a; \
  /opt/workspace-mcp/venv/bin/workspace-mcp --transport streamable-http --read-only'
```
It will print an authorization URL (or trigger one on the first Google call).
Open that URL **in your laptop browser**, sign in, and approve. The redirect to
`localhost:8001` travels through the tunnel to the server, which saves encrypted
tokens under `/var/www/.google_workspace_mcp/credentials/` and refreshes them
automatically afterward. Stop the manual run (Ctrl-C) once consent succeeds.

### Run it as a service

```bash
sudo cp deploy/workspace-mcp.service /etc/systemd/system/workspace-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now workspace-mcp
sudo systemctl status workspace-mcp        # active (running)
curl -s http://127.0.0.1:8001/mcp          # should respond (not connection-refused)
```

## Stage 3 — Point AgentZero at it

In AgentZero's `.env` (`/var/www/production/AgentZero/.env`):
```
MCP_ENABLED=true
GOOGLE_MCP_URL=http://127.0.0.1:8001/mcp
```
```bash
sudo systemctl restart agentzero
sudo journalctl -u agentzero -n 20         # look for "Loaded N MCP tool(s) from google"
```

## Test

Message the bot:
- "what's in my inbox?"
- "any unread emails from this week?"
- "what's on my calendar tomorrow?"

It calls the Google tools, then summarises the results in its own voice.

---

### Notes
- **Read-only is enforced server-side** by `--read-only` — even if the model tried
  to send or delete, those tools aren't exposed.
- Adding more platforms later (GitHub, Slack, Notion) = run another MCP server and
  add its URL to AgentZero's config. The client layer namespaces and routes the
  rest automatically.
- Exact `workspace-mcp` flags can change — check `workspace-mcp --help` and the
  project docs (<https://workspacemcp.com/docs>) if a flag is rejected.
