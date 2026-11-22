"""
Splitch Native TLS / HTTPS & Let's Encrypt Certificate Manager.

Supports three TLS modes with zero extra Python runtime dependencies:
  1. Direct HTTPS with auto-generated SAN self-signed ECDSA P-256 certificates (`splitch tls init`)
  2. Direct HTTPS with custom / enterprise CA or Let's Encrypt certificates (`--ssl-certfile/--ssl-keyfile`
     or `splitch tls letsencrypt <domain>`)
  3. External HTTPS reverse proxy termination (Nginx, NPM, Caddy, Apache, HAProxy, Traefik)
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from backend.router.config import default_data_root

DOMAIN_RE = re.compile(
    r"^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    re.IGNORECASE,
)


def default_certs_dir() -> Path:
    return default_data_root() / "certs"


def _guess_lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))
            ip = str(s.getsockname()[0])
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        return None
    return None


def _chmod_private(path: Path) -> None:
    if sys.platform != "win32":
        try:
            path.chmod(0o600)
        except OSError:
            pass


def generate_self_signed_cert(
    *,
    cert_path: Path | None = None,
    key_path: Path | None = None,
    domains: list[str] | None = None,
    ips: list[str] | None = None,
    days: int = 825,
    force: bool = False,
) -> tuple[Path, Path]:
    """Generate a modern ECDSA P-256 self-signed X.509 certificate with full SANs."""
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError(
            "openssl binary not found on PATH; install OpenSSL or supply --ssl-certfile/--ssl-keyfile"
        )

    certs_dir = default_certs_dir()
    certs_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        try:
            certs_dir.chmod(0o700)
        except OSError:
            pass

    cert_file = (cert_path or (certs_dir / "splitch-selfsigned.crt")).expanduser().resolve()
    key_file = (key_path or (certs_dir / "splitch-selfsigned.key")).expanduser().resolve()
    if cert_file.exists() and key_file.exists() and not force:
        _chmod_private(key_file)
        return cert_file, key_file

    dns_entries: list[str] = ["localhost"]
    hostname = socket.gethostname().strip()
    if hostname and hostname not in dns_entries:
        dns_entries.append(hostname)
        if "." not in hostname:
            dns_entries.append(f"{hostname}.local")
    for d in domains or []:
        cleaned = d.strip()
        if cleaned and cleaned not in dns_entries:
            dns_entries.append(cleaned)

    ip_entries: list[str] = ["127.0.0.1", "::1"]
    lan_ip = _guess_lan_ip()
    if lan_ip and lan_ip not in ip_entries:
        ip_entries.append(lan_ip)
    for raw_ip in ips or []:
        cleaned_ip = raw_ip.strip()
        if not cleaned_ip:
            continue
        # Validate IP syntax strictly so openssl config cannot be injected
        ipaddress.ip_address(cleaned_ip)
        if cleaned_ip not in ip_entries:
            ip_entries.append(cleaned_ip)

    san_lines: list[str] = []
    for idx, d in enumerate(dns_entries, 1):
        if re.search(r"[\r\n,=]", d):
            raise ValueError(f"Invalid DNS SAN entry: {d!r}")
        san_lines.append(f"DNS.{idx} = {d}")
    for idx, ip in enumerate(ip_entries, 1):
        san_lines.append(f"IP.{idx} = {ip}")

    cn = (domains[0].strip() if domains else (lan_ip or hostname or "localhost"))[:64]
    cnf_text = "\n".join(
        [
            "[req]",
            "default_bits = 2048",
            "prompt = no",
            "default_md = sha256",
            "x509_extensions = v3_req",
            "distinguished_name = dn",
            "",
            "[dn]",
            f"CN = {cn}",
            "O = Splitch Local AI",
            "",
            "[v3_req]",
            "basicConstraints = CA:FALSE",
            "keyUsage = critical, digitalSignature, keyEncipherment",
            "extendedKeyUsage = serverAuth",
            "subjectAltName = @alt_names",
            "",
            "[alt_names]",
            *san_lines,
            "",
        ]
    )

    cert_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False, encoding="utf-8") as tf:
        tf.write(cnf_text)
        cnf_path = Path(tf.name)

    try:
        proc = subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-nodes",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-keyout",
                str(key_file),
                "-out",
                str(cert_file),
                "-days",
                str(max(1, min(days, 3650))),
                "-config",
                str(cnf_path),
            ],
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"openssl failed: {proc.stderr.strip() or proc.stdout.strip()}")
    finally:
        cnf_path.unlink(missing_ok=True)

    _chmod_private(key_file)
    if sys.platform != "win32":
        try:
            cert_file.chmod(0o644)
        except OSError:
            pass
    return cert_file, key_file


def find_letsencrypt_cert(domain: str) -> tuple[Path, Path] | None:
    """Locate existing Let's Encrypt certbot or Splitch-managed ACME cert/key for `domain`."""
    clean = domain.strip().lower()
    if not clean or "/" in clean or "\\" in clean or ".." in clean:
        return None
    candidates = [
        (
            Path(f"/etc/letsencrypt/live/{clean}/fullchain.pem"),
            Path(f"/etc/letsencrypt/live/{clean}/privkey.pem"),
        ),
        (
            default_certs_dir() / "letsencrypt" / clean / "fullchain.pem",
            default_certs_dir() / "letsencrypt" / clean / "privkey.pem",
        ),
    ]
    for cert_p, key_p in candidates:
        if cert_p.is_file() and key_p.is_file():
            return cert_p, key_p
    return None


def issue_letsencrypt_cert(
    domain: str,
    *,
    email: str,
    method: str = "standalone",
    webroot: str | None = None,
    staging: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Issue or renew a Let's Encrypt certificate via `certbot`."""
    clean = domain.strip().lower()
    if not DOMAIN_RE.fullmatch(clean):
        raise ValueError(f"Invalid domain name for Let's Encrypt: {domain!r}")
    if "@" not in email or len(email.strip()) < 5:
        raise ValueError("A valid --email address is required by Let's Encrypt ACME")

    certbot = shutil.which("certbot")
    cmd: list[str] = [
        certbot or "certbot",
        "certonly",
        "--non-interactive",
        "--agree-tos",
        "--email",
        email.strip(),
        "-d",
        clean,
    ]
    if method == "webroot":
        if not webroot:
            raise ValueError("--webroot path is required when --method=webroot")
        cmd.extend(["--webroot", "-w", str(Path(webroot).expanduser())])
    else:
        cmd.append("--standalone")
    if staging:
        cmd.append("--staging")

    cert_path = Path(f"/etc/letsencrypt/live/{clean}/fullchain.pem")
    key_path = Path(f"/etc/letsencrypt/live/{clean}/privkey.pem")

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "command": " ".join(cmd),
            "certfile": str(cert_path),
            "keyfile": str(key_path),
        }

    if not certbot:
        raise RuntimeError(
            "certbot not found on PATH. Install certbot (e.g. `sudo apt install certbot` / "
            "`brew install certbot`) or terminate Let's Encrypt via Caddy / Nginx / Traefik."
        )

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180.0, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"certbot failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[-600:]}"
        )
    return {
        "ok": True,
        "dry_run": False,
        "command": " ".join(cmd),
        "certfile": str(cert_path),
        "keyfile": str(key_path),
    }


def inspect_certificate(cert_path: Path) -> dict[str, Any]:
    """Return parsed X.509 certificate metadata via openssl."""
    if not cert_path.is_file():
        return {"exists": False, "path": str(cert_path)}
    openssl = shutil.which("openssl")
    info: dict[str, Any] = {"exists": True, "path": str(cert_path)}
    if not openssl:
        return info
    proc = subprocess.run(
        [
            openssl,
            "x509",
            "-in",
            str(cert_path),
            "-noout",
            "-subject",
            "-issuer",
            "-dates",
            "-ext",
            "subjectAltName",
        ],
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )
    if proc.returncode != 0:
        info["error"] = proc.stderr.strip()
        return info
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith("subject="):
            info["subject"] = ln.removeprefix("subject=").strip()
        elif ln.startswith("issuer="):
            info["issuer"] = ln.removeprefix("issuer=").strip()
        elif ln.startswith("notBefore="):
            info["not_before"] = ln.removeprefix("notBefore=").strip()
        elif ln.startswith("notAfter="):
            info["not_after"] = ln.removeprefix("notAfter=").strip()
        elif "DNS:" in ln or "IP Address:" in ln:
            info["san"] = ln
    return info


def resolve_tls_files(
    *,
    certfile: str | None = None,
    keyfile: str | None = None,
    letsencrypt_domain: str | None = None,
    self_signed: bool = False,
) -> tuple[str | None, str | None]:
    """Resolve effective `(ssl_certfile, ssl_keyfile)` from CLI flags, env vars, or defaults."""
    eff_cert = (certfile or os.getenv("SPLITCH_TLS_CERT", "")).strip()
    eff_key = (keyfile or os.getenv("SPLITCH_TLS_KEY", "")).strip()
    if eff_cert or eff_key:
        if not (eff_cert and eff_key):
            raise ValueError("Both TLS certificate and private key must be provided together")
        cp = Path(eff_cert).expanduser()
        kp = Path(eff_key).expanduser()
        if not cp.is_file():
            raise FileNotFoundError(f"TLS certificate not found: {cp}")
        if not kp.is_file():
            raise FileNotFoundError(f"TLS private key not found: {kp}")
        return str(cp), str(kp)

    le_dom = (letsencrypt_domain or os.getenv("SPLITCH_LETSENCRYPT_DOMAIN", "")).strip()
    if le_dom:
        found = find_letsencrypt_cert(le_dom)
        if not found:
            raise FileNotFoundError(
                f"No Let's Encrypt cert found for '{le_dom}'. "
                f"Run `splitch tls letsencrypt {le_dom} --email you@example.com` first."
            )
        return str(found[0]), str(found[1])

    env_self = os.getenv("SPLITCH_TLS_SELF_SIGNED", "").strip().lower() in ("1", "true", "yes")
    if self_signed or env_self:
        cp, kp = generate_self_signed_cert()
        return str(cp), str(kp)

    return None, None
