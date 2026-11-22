# Splitch — Nginx Proxy Manager (NPM) Setup

Run Splitch with:
```bash
export SPLITCH_API_KEY="your-strong-random-secret"
export SPLITCH_ALLOWED_ORIGINS="https://ai.example.com"
export SPLITCH_TRUSTED_PROXIES="127.0.0.1,::1,172.16.0.0/12,192.168.0.0/16"
splitch router --host 0.0.0.0 --port 8080
```

## NPM Proxy Host Settings

1. **Details Tab:**
   - **Domain Names:** `ai.example.com`
   - **Scheme:** `http`
   - **Forward Hostname / IP:** `<SPLITCH_ROUTER_IP>` (or `host.docker.internal` if NPM is in Docker on the same host)
   - **Forward Port:** `8080`
   - **Cache Assets:** `OFF`
   - **Block Common Exploits:** `ON`
   - **Websockets Support:** `ON` *(required for `/ws/status`)*

2. **SSL Tab:**
   - **SSL Certificate:** Choose **Request a new SSL Certificate** (Let's Encrypt) **or** select an uploaded Custom Certificate.
   - **Force SSL:** `ON`
   - **HTTP/2 Support:** `ON`
   - **HSTS Enabled:** `ON`

3. **Advanced Tab (Custom Nginx Configuration — required for unbuffered SSE & long generations):**
   ```nginx
   client_max_body_size 100m;
   proxy_buffering off;
   proxy_cache off;
   proxy_read_timeout 600s;
   proxy_send_timeout 600s;
   proxy_set_header X-Forwarded-Proto $scheme;
   ```
