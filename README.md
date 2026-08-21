# Secure Tailscale LLM Proxy Rotator

An intelligent API gateway for local AI coding agents that distributes LLM requests across multiple Tailscale nodes with automatic failover, key injection, and advanced token optimization.

## 🚀 Features

- **Dynamic Key Injection**: Automatically injects the correct API key matched to the selected egress Tailscale node
- **Resilient Failover**: Seamlessly retries failed requests (429, 5xx, timeouts) on the next available node
- **Token Optimization**: Advanced context compression to maximize token usage and reduce waste
- **Environment Configuration**: No hardcoded secrets - all configuration via environment variables
- **DNS Leak Prevention**: Uses `socks5h://` protocol to ensure DNS resolution happens on remote nodes
- **Thread-Safe Rotation**: Atomic round-robin node selection for concurrent requests
- **Streaming Support**: Full support for streaming chat completions

## 📋 Prerequisites

- Python 3.8+
- Tailscale installed and authenticated
- Four (or more) Tailscale nodes running SOCKS5 proxies
- API keys for each node's LLM provider account

## 🔧 Installation

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment variables:**
   ```bash
   cp .env.example .env
   # Edit .env with your actual values
   ```

3. **Required environment variables:**
   ```bash
   # Node configuration (add as many as needed)
   PROXY_1_URL=socks5h://100.64.0.1:1055
   API_KEY_1=sk-proj-your-actual-key-1
   
   PROXY_2_URL=socks5h://100.64.0.2:1055
   API_KEY_2=sk-proj-your-actual-key-2
   
   # ... add more nodes as needed
   ```

## 🎯 Token Optimization Features

The proxy includes advanced token management to maximize context utilization:

### Automatic Optimizations (enabled by default):

1. **Duplicate Message Removal**: Eliminates consecutive duplicate messages
2. **Whitespace Compression**: Strips excessive whitespace and newlines
3. **Smart Truncation**: Removes oldest messages when approaching token limits
4. **Context-Aware Max Tokens**: Dynamically adjusts `max_tokens` based on available context

### Configuration Options:

```bash
# Enable/disable optimization
ENABLE_CONTEXT_COMPRESSION=true

# Set context window size (reserve space for response)
MAX_CONTEXT_TOKENS=120000
RESERVED_RESPONSE_TOKENS=4000

# Compress when context is >85% full
COMPRESSION_THRESHOLD=0.85

# Toggle specific optimizations
REMOVE_DUPLICATE_MESSAGES=true
STRIP_WHITESPACE=true
```

## 🏃 Running the Proxy

### Direct execution:
```bash
python rotator.py
```

### With systemd (Linux):
```bash
sudo cp llm-rotator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable llm-rotator
sudo systemctl start llm-rotator
```

## 📡 Endpoints

- `POST /v1/chat/completions` - Main chat completion endpoint
- `GET /v1/models` - List available models
- `GET /health` - Health check with status info

## 🔒 Security

1. **Tailscale ACLs**: Configure your Tailnet to restrict proxy access
2. **Localhost Binding**: The proxy binds to `127.0.0.1` by default
3. **No Secret Logging**: API keys are never logged (only prefixes shown)

## 📊 Monitoring

Check the health endpoint:
```bash
curl http://127.0.0.1:8080/health
```

## 🔧 IDE Integration

Configure your AI coding assistant to use:
```json
{
  "models": [{
    "title": "Tailscale Rotated LLM Pool",
    "provider": "openai",
    "model": "gpt-4o",
    "apiBase": "http://127.0.0.1:8080/v1",
    "apiKey": "managed-by-local-rotator-daemon"
  }]
}
```

## 📝 Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXY_BIND_HOST` | `127.0.0.1` | Host to bind the proxy server |
| `PROXY_BIND_PORT` | `8080` | Port to bind the proxy server |
| `LLM_PROVIDER_URL` | `https://api.openai.com/v1` | Target LLM provider API URL |
| `REQUEST_TIMEOUT` | `25.0` | Request timeout in seconds |
| `MAX_RETRIES` | `4` | Maximum retry attempts across nodes |
| `ENABLE_CONTEXT_COMPRESSION` | `true` | Enable token optimization |
| `MAX_CONTEXT_TOKENS` | `120000` | Maximum context window size |
| `RESERVED_RESPONSE_TOKENS` | `4000` | Tokens reserved for response |
| `COMPRESSION_THRESHOLD` | `0.85` | Compression trigger threshold |
| `LOG_LEVEL` | `INFO` | Logging level |

## ⚠️ Troubleshooting

**All nodes failing?**
- Verify Tailscale is running: `tailscale status`
- Check SOCKS5 proxy is listening: `netstat -tlnp | grep 1055`

**Token optimization not working?**
- Ensure `tiktoken` is installed: `pip install tiktoken`
- Check logs for optimization stats

