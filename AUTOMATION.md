# LinkedIn automation — 2FA email via Google MCP

## What changed

When Google 2FA appears during LinkedIn login, the scanner now:

1. Prints `TAP NUMBER:` / `TAP YES:` (unchanged)
2. Writes `/workspace/tfa_alert.json` with tap details
3. Calls your **Google/Gmail MCP** via `gmail_mcp_notify.py` to email you the tap number

**Your only action:** approve 2FA on your phone when the email arrives.

## MCP setup (one-time)

1. In [cursor.com/agents](https://cursor.com/agents) → **MCP**, ensure your Google/Gmail MCP is enabled for this automation.
2. Add Cloud Agent secrets for script-side Gmail MCP (match your Cursor Google MCP OAuth app):
   - `GOOGLE_ACCESS_TOKEN` — OAuth access token with Gmail send scope (recommended for cron)
   - Or `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` if your MCP server uses them
   - Optional: `TFA_NOTIFY_EMAIL` (defaults to `LINKEDIN_EMAIL`)
   - Optional: `GMAIL_MCP_URL` — HTTP MCP endpoint if your Google MCP uses HTTP transport
3. Copy and customize MCP config:
   ```bash
   mkdir -p .cursor
   cp .cursor/mcp.example.json .cursor/mcp.json
   ```
   Use the same `command` / `args` / `url` as your Google MCP in Cursor.

## Automation prompt addition

Add this block to your cron automation instructions:

```
When TFA_ALERT| appears in script output (or tfa_alert.json is written):
1. IMMEDIATELY use Google/Gmail MCP to send an email to LINKEDIN_EMAIL
2. Subject: "LinkedIn login: TAP NUMBER {num}" (or TAP YES)
3. Body: include tap number, device name, and "You have 5 minutes"
4. Do not wait for user browser clicks — only mobile 2FA approval is allowed
```

## Supported MCP tools

The notifier auto-detects send tools, including:
- `send_email`
- `gmail_send`
- `gmail_message_send`

## Verify

```bash
python3 gmail_mcp_notify.py 42
```

Check your inbox for a test email with tap number **42**.
