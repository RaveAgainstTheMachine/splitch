# HTTPS, TLS & External Reverse Proxies

Splitch supports three HTTPS deployment modes:
1. **Direct Native HTTPS (Without Let's Encrypt)** — auto-generated ECDSA P-256 SAN self-signed certificates (`splitch tls init`) or your own internal CA / `mkcert` / enterprise PEM certificates.
2. **Direct Native HTTPS (With Let's Encrypt)** — ACME certificates via `certbot` (`splitch tls letsencrypt <domain>`) auto-loaded by `splitch router --letsencrypt-domain <domain>`.
3. **External HTTPS Reverse Proxy Termination** — full proxy-header awareness (`X-Forwarded-Proto`, `X-Forwarded-For`), automatic `Secure` cookies, HSTS, unbuffered SSE (`X-Accel-Buffering: no`), and WebSockets (`/ws/status`) behind **Nginx**, **Nginx Proxy Manager**, **Caddy**, **Apache**, **HAProxy**, **Traefik**, **Tailscale Serve**, or **Cloudflare Tunnel**.

---

## 1. Direct Native HTTPS — Without Let's Encrypt

### Option A: Built-in Self-Signed SAN Certificate (LAN / Homelab)
Generate an ECDSA P-256 certificate containing `localhost`, your hostname, `127.0.0.1`, `::1`, your LAN IP, plus any custom domains/IPs (private key saved with `0600` permissions):

```bash
splitch tls init --domain splitch.local --ip 192.168.1.50
splitch tls status
```

Launch with HTTPS:
```bash
# Auto-generates cert if missing and serves HTTPS directly
splitch router --tls-self-signed --port 8443

# Or single-node all-in-one
splitch up --tls-self-signed --port 8443
```

Or via `splitch.toml`:
```toml
[tls]
enabled = true
self_signed = true
```

### Option B: Custom Certificate / Local CA (`mkcert`, Step-CA, Enterprise PKI)
Generate a locally trusted cert with [`mkcert`](https://github.com/FiloSottile/mkcert) (zero browser warning):
```bash
mkcert -install
mkcert -cert-file ~/.local/share/splitch/certs/splitch.crt \
       -key-file  ~/.local/share/splitch/certs/splitch.key \
       localhost 127.0.0.1 ::1 splitch.local 192.168.1.50
chmod 600 ~/.local/share/splitch/certs/splitch.key
```

Run Splitch with the cert & key:
```bash
splitch router \
  --ssl-certfile ~/.local/share/splitch/certs/splitch.crt \
  --ssl-keyfile  ~/.local/share/splitch/certs/splitch.key \
  --port 8443
```

Or via environment variables / `splitch.toml`:
```bash
export SPLITCH_TLS_CERT="$HOME/.local/share/splitch/certs/splitch.crt"
export SPLITCH_TLS_KEY="$HOME/.local/share/splitch/certs/splitch.key"
splitch router --port 8443
```

---

## 2. Direct Native HTTPS — With Let's Encrypt

Issue a Let's Encrypt certificate via `certbot`:
```bash
# Standalone mode (port 80 temporarily free)
sudo splitch tls letsencrypt ai.example.com --email admin@example.com --method standalone

# Or webroot mode (existing web server serves /.well-known/acme-challenge/)
sudo splitch tls letsencrypt ai.example.com --email admin@example.com --method webroot --webroot /var/www/certbot
```

Run Splitch directly using the Let's Encrypt live symlink:
```bash
export SPLITCH_API_KEY="$(openssl rand -hex 32)"
export SPLITCH_ALLOWED_ORIGINS="https://ai.example.com"
splitch router --letsencrypt-domain ai.example.com --port 443
```

Or in `splitch.toml`:
```toml
[tls]
enabled = true
letsencrypt_domain = "ai.example.com"

[security]
allowed_origins = ["https://ai.example.com"]
hsts_enabled = true
```

**Automatic renewal hook (systemd):**
```bash
# /etc/letsencrypt/renewal-hooks/deploy/splitch-reload.sh
#!/bin/sh
systemctl reload-or-restart splitch-router.service 2>/dev/null || true
```

---

## 3. External HTTPS Reverse Proxies

When terminating TLS at a reverse proxy:
1. Bind Splitch to localhost (`--host 127.0.0.1 --port 8080`) if the proxy runs on the same host, or firewall port `8080` to only accept connections from your proxy IP.
2. Always enable `SPLITCH_API_KEY`, set `SPLITCH_ALLOWED_ORIGINS` to your public origin(s), and configure `SPLITCH_TRUSTED_PROXIES`:
   ```bash
   export SPLITCH_API_KEY="$(openssl rand -hex 32)"
   export SPLITCH_ALLOWED_ORIGINS="https://ai.example.com"
   export SPLITCH_TRUSTED_PROXIES="127.0.0.1,::1"   # or proxy LAN IP / Docker subnet
   splitch router --host 127.0.0.1 --port 8080
   ```
3. Splitch automatically emits `Cache-Control: no-cache` and `X-Accel-Buffering: no` on SSE responses, sets `Secure; HttpOnly; SameSite=Strict` on the `splitch_session` cookie, and sends `Strict-Transport-Security` when `X-Forwarded-Proto: https` is present.

### Proxy Compatibility & Templates

| Proxy | Automatic Let's Encrypt | Custom / Self-Signed | SSE Unbuffered | WebSocket (`/ws/status`) | Template |
| --- | --- | --- | --- | --- | --- |
| **Nginx** | Certbot (`--nginx` / webroot) | `ssl_certificate` | `proxy_buffering off` | `Upgrade` / `Connection` | [nginx.conf](../../infra/proxies/nginx.conf) |
| **Nginx Proxy Manager** | Built-in UI toggle | Custom Cert upload | Advanced tab snippet | Websockets toggle ON | [nginx-proxy-manager.md](../../infra/proxies/nginx-proxy-manager.md) |
| **Caddy** | Zero-config automatic | `tls internal` / PEM | `flush_interval -1` | Automatic | [Caddyfile](../../infra/proxies/Caddyfile) |
| **Apache** | Certbot (`--apache`) | `SSLCertificateFile` | `flushpackets=on` | `mod_proxy_wstunnel` | [apache-vhost.conf](../../infra/proxies/apache-vhost.conf) |
| **HAProxy** | Certbot + combined PEM | `ssl crt ...pem` | `option http-no-delay` | `timeout tunnel 3600s` | [haproxy.cfg](../../infra/proxies/haproxy.cfg) |
| **Traefik v3** | `certResolver` ACME | `tls.certificates` | `flushInterval: "-1"` | Automatic | [traefik-dynamic.yml](../../infra/proxies/traefik-dynamic.yml) |

### Nginx

Full file: [infra/proxies/nginx.conf](../../infra/proxies/nginx.conf)

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl;
    http2 on;
    server_name ai.example.com;

    ssl_certificate     /etc/letsencrypt/live/ai.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ai.example.com/privkey.pem;
    client_max_body_size 100m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade           $http_upgrade;
        proxy_set_header Connection        $connection_upgrade;
        proxy_buffering off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
```

### Nginx Proxy Manager (NPM)

Full guide: [infra/proxies/nginx-proxy-manager.md](../../infra/proxies/nginx-proxy-manager.md)

- **Details:** Forward to `http://<SPLITCH_IP>:8080`, **Cache Assets OFF**, **Websockets Support ON**.
- **SSL:** Request Let's Encrypt certificate (or select Custom Cert), enable **Force SSL**, **HTTP/2**, **HSTS**.
- **Advanced tab:**
  ```nginx
  client_max_body_size 100m;
  proxy_buffering off;
  proxy_cache off;
  proxy_read_timeout 600s;
  proxy_send_timeout 600s;
  proxy_set_header X-Forwarded-Proto $scheme;
  ```

### Caddy

Full file: [infra/proxies/Caddyfile](../../infra/proxies/Caddyfile)

```caddyfile
# Automatic Let's Encrypt HTTPS
ai.example.com {
    reverse_proxy 127.0.0.1:8080 {
        flush_interval -1
        transport http {
            read_timeout 600s
            write_timeout 600s
        }
    }
}

# Or internal LAN HTTPS without Let's Encrypt:
# splitch.local {
#     tls internal
#     reverse_proxy 127.0.0.1:8080 {
#         flush_interval -1
#     }
# }
```

### Apache HTTP Server

Full file: [infra/proxies/apache-vhost.conf](../../infra/proxies/apache-vhost.conf)

```apache
# sudo a2enmod ssl proxy proxy_http proxy_wstunnel headers rewrite
<VirtualHost *:443>
    ServerName ai.example.com
    SSLEngine on
    SSLCertificateFile    /etc/letsencrypt/live/ai.example.com/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/ai.example.com/privkey.pem

    ProxyPreserveHost On
    ProxyTimeout 600
    RequestHeader set X-Forwarded-Proto "https"

    ProxyPass        /ws/status ws://127.0.0.1:8080/ws/status
    ProxyPassReverse /ws/status ws://127.0.0.1:8080/ws/status
    ProxyPass        / http://127.0.0.1:8080/ flushpackets=on timeout=600
    ProxyPassReverse / http://127.0.0.1:8080/
</VirtualHost>
```

### HAProxy

Full file: [infra/proxies/haproxy.cfg](../../infra/proxies/haproxy.cfg)

```haproxy
frontend https_in
    bind *:443 ssl crt /etc/haproxy/certs/ai.example.com.pem alpn h2,http/1.1
    http-request set-header X-Forwarded-Proto https if { ssl_fc }
    default_backend splitch_backend

backend splitch_backend
    option httpchk GET /health
    option http-no-delay
    server splitch1 127.0.0.1:8080 check inter 5s
```

### Traefik v3

Full file: [infra/proxies/traefik-dynamic.yml](../../infra/proxies/traefik-dynamic.yml)

```yaml
http:
  routers:
    splitch-https:
      entryPoints: [websecure]
      rule: "Host(`ai.example.com`)"
      service: splitch-svc
      tls:
        certResolver: letsencrypt
  services:
    splitch-svc:
      loadBalancer:
        responseForwarding:
          flushInterval: "-1"
        servers:
          - url: "http://127.0.0.1:8080"
```

### Tailscale Serve (Zero-Port-Forward Private HTTPS)

```bash
# Serves Splitch over valid Let's Encrypt HTTPS on your Tailnet (*.ts.net)
tailscale serve --bg http://127.0.0.1:8080
```

---

## 4. Encrypting Router ↔ Node Agent Traffic (Optional Internal TLS)

You can also run `splitch-agent` over HTTPS on remote compute nodes:

1. Generate/install a TLS cert on the compute node (with the node's LAN/Tailscale IP SAN):
   ```bash
   splitch tls init --ip 192.168.1.50
   SPLITCH_AGENT_TOKEN="shared-secret" \
     splitch agent --ssl-certfile ~/.local/share/splitch/certs/splitch-selfsigned.crt \
                   --ssl-keyfile  ~/.local/share/splitch/certs/splitch-selfsigned.key
   ```
2. On the router host, trust the node/internal CA cert via `SPLITCH_CA_BUNDLE` (or `[tls].ca_bundle` in `splitch.toml`) and use `https://` in `agent_url`:
   ```bash
   export SPLITCH_AGENT_TOKEN="shared-secret"
   export SPLITCH_CA_BUNDLE="/path/to/lan-ca.pem"
   ```

---

## 5. Production Hardening Checklist

- [ ] Set `SPLITCH_API_KEY` (32+ random bytes) — protects `/v1/*`, `/api/*`, `/infill`, `/ws/status`, and enforces the Web UI login gate with per-IP brute-force rate limiting (`429` after 10 failures / 60s).
- [ ] Set `SPLITCH_AGENT_TOKEN` identically on router and every `splitch-agent`.
- [ ] Keep `SPLITCH_ENABLE_TOOLS=0` unless a specific trusted workstation needs Agent Chat workspace tools; always restrict `--workspace` to a dedicated project folder.
- [ ] Bind `splitch router --host 127.0.0.1` when a reverse proxy runs on the same machine.
- [ ] Set `SPLITCH_ALLOWED_ORIGINS="https://your-domain"` and `SPLITCH_TRUSTED_PROXIES` to only your proxy's IP/CIDR.
- [ ] Run `splitch doctor` to verify security, TLS, and permissions posture.
