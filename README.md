# Secure LLM Proxy Rotator - Production Deployment Guide

## Overview
This system acts as an intelligent API gateway that distributes LLM requests across 
multiple Tailscale nodes, each with unique API keys and outbound IPs. Features include:
- Dynamic API key injection per node
- Automatic failover on rate limits (429) or server errors (5xx)
- Thread-safe round-robin rotation
- SOCKS5H proxy routing (DNS resolution on remote nodes)

---

## Prerequisites

1. **Tailscale installed** on all nodes (client + 4 gateway nodes)
2. **Python 3.8+** with pip on the client machine
3. **Four Tailscale nodes** configured as SOCKS5 proxies
4. **Four separate LLM provider accounts** (one per node)

---

## Step 1: Configure Tailscale ACLs

1. Log into your [Tailscale Admin Console](https://login.tailscale.com/admin)
2. Navigate to **Settings → Access Controls**
3. Replace the existing ACL JSON with the contents of `tailscale_acl.json`
4. Update `admin@yourdomain.com` with your actual Tailscale user email
5. Save the configuration

---

## Step 2: Set Up Gateway Nodes

On **each of the four gateway nodes**, run:

```bash
# Install Tailscale if not already installed
curl -fsSL https://tailscale.com/install.sh | sh

# Start tailscaled with SOCKS5 proxy enabled on port 1055
sudo tailscaled --socks5-server=0.0.0.0:1055

# Verify the proxy is listening
sudo netstat -tlnp | grep 1055
# Expected output: tcp  0  0 0.0.0.0:1055  0.0.0.0:*  LISTEN  <pid>/tailscaled
```

**Optional:** Create a systemd service for persistent SOCKS5 proxy:

```bash
# /etc/systemd/system/tailscale-socks5.service
[Unit]
Description=Tailscale with SOCKS5 Proxy
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/tailscaled --socks5-server=0.0.0.0:1055
Restart=always

[Install]
WantedBy=multi-user.target
```

Then enable it:
```bash
sudo systemctl daemon-reload
sudo systemctl enable tailscale-socks5
sudo systemctl start tailscale-socks5
```

---

## Step 3: Install Dependencies (Client Machine)

```bash
cd /workspace
pip install flask requests
```

---

## Step 4: Configure the Rotator

Edit `rotator.py` and update the `NODE_POOL` configuration:

```python
NODE_POOL = [
    {"proxy": "socks5h://100.64.0.1:1055", "api_key": "sk-proj-YourActualKey1"},
    {"proxy": "socks5h://100.64.0.2:1055", "api_key": "sk-proj-YourActualKey2"},
    {"proxy": "socks5h://100.64.0.3:1055", "api_key": "sk-proj-YourActualKey3"},
    {"proxy": "socks5h://100.64.0.4:1055", "api_key": "sk-proj-YourActualKey4"}
]
```

**To find your Tailscale node IPs:**
```bash
tailscale status
# Look for IPs in the 100.64.x.x range
```

---

## Step 5: Test the Proxy Manually

```bash
# Start the rotator in the foreground
python3 rotator.py

# In another terminal, test connectivity
curl http://127.0.0.1:8080/health
# Expected: OK

curl http://127.0.0.1:8080/status
# Expected: Active nodes, max retries, target URL, port

# Test an actual LLM request
curl -X POST http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

---

## Step 6: Daemonize with systemd (Production)

1. **Create the application directory:**
   ```bash
   sudo mkdir -p /opt/llm-rotator
   sudo cp rotator.py /opt/llm-rotator/
   sudo cp llm-rotator.service /etc/systemd/system/
   ```

2. **Update the service file:**
   ```bash
   sudo nano /etc/systemd/system/llm-rotator.service
   # Change "your-username" to your actual username
   ```

3. **Enable and start the service:**
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable llm-rotator
   sudo systemctl start llm-rotator
   ```

4. **Verify it's running:**
   ```bash
   sudo systemctl status llm-rotator
   journalctl -u llm-rotator -f  # View logs
   ```

---

## Step 7: Configure Your Coding Agent

### For Continue.dev (`~/.continue/config.json`):
```json
{
  "models": [
    {
      "title": "Tailscale Rotated LLM Pool",
      "provider": "openai",
      "model": "gpt-4o",
      "apiBase": "http://127.0.0.1:8080/v1",
      "apiKey": "managed-by-local-rotator-daemon"
    }
  ]
}
```

### For Cursor:
1. Go to **Settings → Models**
2. Add custom OpenAI-compatible endpoint: `http://127.0.0.1:8080/v1`
3. Use any placeholder API key (the rotator injects the real one)

### For VS Code + OpenCode Extension:
Use the provided `config.json` in this repository.

---

## Troubleshooting

### All nodes failing with connection errors
- Verify Tailscale is running on gateway nodes: `tailscale status`
- Check SOCKS5 proxy is listening: `netstat -tlnp | grep 1055`
- Test connectivity: `curl --socks5-hostname 100.64.0.1:1055 https://api.openai.com`

### 429 errors still occurring
- Ensure each node has a **different** API key
- Verify ACLs allow traffic from your client tag to proxy nodes
- Check that `MAX_RETRIES` matches your pool size (default: 4)

### DNS leaks detected
- Confirm you're using `socks5h://` (not `socks5://`) in NODE_POOL
- The 'h' suffix forces DNS resolution through the proxy

### Service won't start
- Check logs: `journalctl -u llm-rotator -n 50`
- Verify Python dependencies: `pip3 list | grep -E "flask|requests"`
- Ensure port 8080 isn't in use: `sudo lsof -i :8080`

---

## Security Checklist

- [x] Tailscale ACLs restrict access to tagged nodes only
- [x] Rotator binds to localhost (127.0.0.1) only
- [x] SOCKS5H ensures no local DNS queries
- [x] API keys never exposed to client applications
- [ ] Consider adding HTTPS with TLS termination (future enhancement)
- [ ] Implement rate limiting on the local endpoint (future enhancement)

---

## Architecture Diagram

```
┌─────────────────┐
│  Coding Agent   │
│  (Continue/Cursor)│
└────────┬────────┘
         │ HTTP to 127.0.0.1:8080
         ▼
┌─────────────────────────────────────┐
│   Local Proxy Rotator (rotator.py)  │
│  • Thread-safe round-robin          │
│  • Key injection per node           │
│  • Automatic failover on 429/5xx    │
└────────┬────────────────────────────┘
         │ Encrypted Tailscale Tunnel
         ▼
┌─────────────────────────────────────────────────┐
│              Tailnet (Encrypted)                │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │ Node 1   │  │ Node 2   │  │ Node 3   │ ...  │
│  │100.64.0.1│  │100.64.0.2│  │100.64.0.3│      │
│  │SOCKS5H   │  │SOCKS5H   │  │SOCKS5H   │      │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘      │
│       │             │             │             │
│       ▼             ▼             ▼             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │ Account A│  │ Account B│  │ Account C│      │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘      │
│       │             │             │             │
│       └─────────────┴─────────────┘             │
│                     │                           │
└─────────────────────┼───────────────────────────┘
                      ▼
            ┌─────────────────┐
            │  LLM Provider   │
            │ (OpenAI/Anthropic)│
            └─────────────────┘
```

---

## License
MIT License - See LICENSE file for details
