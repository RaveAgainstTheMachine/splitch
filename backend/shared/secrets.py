"""
Cryptographic secrets management, salted hashing, AES-256-GCM local vault,
`env:`/`file:`/`vault:` config indirection, and Ed25519 asymmetric router <-> agent signing.

Security invariants:
  - Verifier-only secrets (`SPLITCH_API_KEY_HASH`, join tokens) are salted & hashed; plaintext is never persisted.
  - Session cookies are signed with an independent CSPRNG secret (`data_dir/.session_secret`, 0600).
  - Outbound / UI-entered secrets (MCP env tokens, search API keys) are encrypted at rest in
    `data_dir/secrets.enc` with AES-256-GCM and AAD bound to the secret name.
  - `file:` secret pointers enforce POSIX `0600`-compatible permissions (`mode & 0o077 == 0`).
  - Ed25519 asymmetric signing lets the router hold the private key (0600) while worker agents
    store only the router's public key (`ed25519:<base64>`), eliminating shared symmetric secrets.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets as py_secrets
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.shared.security import _canonical_tool_payload

MASKED_SECRET_SENTINEL = "••••••••"

_ENV_BRACE_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_SECRET_KEY_SUBSTRINGS = (
    "TOKEN",
    "SECRET",
    "KEY",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
    "BEARER",
    "PRIVATE",
    "PAT",
)
_SECRET_KEY_PREFIXES = (
    "GITHUB_",
    "BRAVE_",
    "TAVILY_",
    "OPENAI_",
    "ANTHROPIC_",
    "SLACK_",
    "NOTION_",
    "LINEAR_",
    "SENTRY_",
    "SUPABASE_",
    "CLOUDFLARE_",
    "UPSTASH_",
    "EXA_",
    "FIRECRAWL_",
    "HF_",
)
_VAULT_NAME_RE = re.compile(r"^[A-Za-z0-9._:/\-]{1,128}$")

# Non-plaintext verification cache for slow scrypt hashes (keyed by process-ephemeral HMAC)
_CACHE_LOCK = threading.Lock()
_CACHE_KEY = py_secrets.token_bytes(32)
_VERIFIED_CACHE: dict[bytes, float] = {}
_VERIFIED_CACHE_TTL_S = 60.0
_VERIFIED_CACHE_MAX = 64


# ── Atomic Private File I/O ──────────────────────────────────────────────────


def write_private_bytes(path: Path, data: bytes) -> None:
    """Atomically write `data` to `path` with strict owner-only permissions (0600 on POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{py_secrets.token_hex(6)}.tmp"
    if sys.platform != "win32":
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        with contextlib.suppress(OSError):
            os.chmod(str(tmp), 0o600)
    else:
        tmp.write_bytes(data)
    tmp.replace(path)
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            path.chmod(0o600)


def write_private_text(path: Path, text: str) -> None:
    write_private_bytes(path, text.encode("utf-8"))


def _assert_private_file_permissions(path: Path) -> None:
    """Reject group/world-readable secret files on POSIX."""
    if sys.platform == "win32":
        return
    st = path.stat()
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise PermissionError(
            f"Secret file '{path}' has unsafe permissions ({oct(stat.S_IMODE(st.st_mode))}); "
            "run `chmod 600` to restrict it to owner-only."
        )


# ── Salted Secret Hashing (scrypt & SHA-256) ─────────────────────────────────


def hash_secret_scrypt(
    plaintext: str,
    *,
    n: int = 2**14,
    r: int = 8,
    p: int = 1,
) -> str:
    """Hash a human password or API key with scrypt + 16-byte random salt."""
    if not plaintext:
        raise ValueError("Secret to hash must not be empty")
    salt = py_secrets.token_bytes(16)
    dk = hashlib.scrypt(
        plaintext.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=32,
    )
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    dk_b64 = base64.urlsafe_b64encode(dk).decode("ascii").rstrip("=")
    return f"scrypt${n}${r}${p}${salt_b64}${dk_b64}"


def hash_secret_sha256(plaintext: str) -> str:
    """Fast salted SHA-256 hash for high-entropy random tokens (>=192 bits, e.g. join tokens)."""
    if not plaintext:
        raise ValueError("Token to hash must not be empty")
    salt = py_secrets.token_bytes(16)
    digest = hashlib.sha256(salt + plaintext.encode("utf-8")).digest()
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    dg_b64 = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"sha256${salt_b64}${dg_b64}"


def _b64_decode_nopad(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def verify_secret_hash(candidate: str, stored_hash: str) -> bool:
    """Constant-time verification for `scrypt$...` and `sha256$...` salted hashes."""
    if not candidate or not stored_hash:
        return False
    parts = stored_hash.strip().split("$")
    algo = parts[0].lower()

    if algo == "sha256" and len(parts) == 3:
        try:
            salt = _b64_decode_nopad(parts[1])
            expected = _b64_decode_nopad(parts[2])
        except ValueError:
            return False
        actual = hashlib.sha256(salt + candidate.encode("utf-8")).digest()
        return hmac.compare_digest(actual, expected)

    if algo == "scrypt" and len(parts) == 6:
        cache_tag = hmac.new(
            _CACHE_KEY,
            f"{stored_hash}\x00{candidate}".encode(),
            hashlib.sha256,
        ).digest()
        now = time.monotonic()
        with _CACHE_LOCK:
            exp = _VERIFIED_CACHE.get(cache_tag)
            if exp is not None and exp > now:
                return True
        try:
            n = int(parts[1])
            r = int(parts[2])
            p = int(parts[3])
            salt = _b64_decode_nopad(parts[4])
            expected = _b64_decode_nopad(parts[5])
            if n < 1024 or n > 2**20 or (n & (n - 1)) != 0 or r < 1 or p < 1:
                return False
            actual = hashlib.scrypt(
                candidate.encode("utf-8"),
                salt=salt,
                n=n,
                r=r,
                p=p,
                maxmem=128 * 1024 * 1024,
                dklen=len(expected),
            )
        except (ValueError, MemoryError):
            return False
        ok = hmac.compare_digest(actual, expected)
        if ok:
            with _CACHE_LOCK:
                if len(_VERIFIED_CACHE) >= _VERIFIED_CACHE_MAX:
                    # Prune expired entries, or clear oldest
                    stale = [k for k, v in _VERIFIED_CACHE.items() if v <= now]
                    for k in stale:
                        _VERIFIED_CACHE.pop(k, None)
                    if len(_VERIFIED_CACHE) >= _VERIFIED_CACHE_MAX:
                        _VERIFIED_CACHE.clear()
                _VERIFIED_CACHE[cache_tag] = now + _VERIFIED_CACHE_TTL_S
        return ok

    return False


# ── Independent Session Secret ───────────────────────────────────────────────


def load_or_create_session_secret(data_dir: Path) -> str:
    """
    Return the router's independent session-signing secret.
    Checks `SPLITCH_SESSION_SECRET` (supports `env:`/`file:`), else loads or creates
    `data_dir/.session_secret` (0600).
    """
    env_raw = os.getenv("SPLITCH_SESSION_SECRET", "").strip()
    if env_raw:
        resolved = resolve_secret_value(env_raw, check_permissions=True)
        if resolved:
            return resolved
    sec_file = data_dir / ".session_secret"
    if sec_file.is_file():
        try:
            existing = sec_file.read_text(encoding="utf-8").strip()
            if len(existing) >= 32:
                return existing
        except OSError:
            pass
    fresh = py_secrets.token_hex(32)
    with contextlib.suppress(OSError):
        write_private_text(sec_file, fresh + "\n")
    return fresh


# ── AES-256-GCM Encrypted Secret Vault ───────────────────────────────────────


def _derive_32b_key(raw: str) -> bytes:
    cleaned = raw.strip()
    # Try raw 64-char hex (32 bytes)
    if len(cleaned) == 64:
        with contextlib.suppress(ValueError):
            return bytes.fromhex(cleaned)
    # Try base64/urlsafe-base64 decoding to exactly 32 bytes
    with contextlib.suppress(ValueError):
        decoded = _b64_decode_nopad(cleaned)
        if len(decoded) == 32:
            return decoded
    # Derive deterministic 32-byte key from arbitrary passphrase via SHA-256 domain separation
    return hashlib.sha256(b"splitch-master-key-v1:" + cleaned.encode("utf-8")).digest()


def _try_keyring_master_key(service_name: str = "splitch-vault") -> bytes | None:
    """Optionally use OS Keychain / SecretService / Windows Credential Locker if `keyring` is installed."""
    if os.getenv("SPLITCH_USE_KEYRING", "0").strip().lower() not in ("1", "true", "yes"):
        return None
    try:
        import keyring  # type: ignore[import-not-found]

        existing = keyring.get_password(service_name, "master_key")
        if existing:
            return _derive_32b_key(existing)
        fresh = py_secrets.token_hex(32)
        keyring.set_password(service_name, "master_key", fresh)
        return bytes.fromhex(fresh)
    except (ImportError, OSError, RuntimeError, ValueError):
        return None


class SecretVault:
    """
    AES-256-GCM encrypted local secret store (`data_dir/secrets.enc`, 0600).
    Each secret entry is encrypted with a fresh 96-bit nonce and AAD bound to its name.
    """

    def __init__(self, data_dir: Path, *, master_key: bytes | None = None) -> None:
        self.data_dir = data_dir
        self.vault_path = data_dir / "secrets.enc"
        self.key_path = data_dir / ".master.key"
        self._explicit_key = master_key
        self._lock = threading.Lock()

    def _resolve_master_key(self, *, create: bool = True) -> bytes:
        if self._explicit_key is not None:
            return self._explicit_key

        env_key = os.getenv("SPLITCH_MASTER_KEY", "").strip()
        if env_key:
            resolved = resolve_secret_value(env_key, check_permissions=True)
            if resolved:
                return _derive_32b_key(resolved)

        key_file_env = os.getenv("SPLITCH_MASTER_KEY_FILE", "").strip()
        if not key_file_env:
            cred_dir = os.getenv("CREDENTIALS_DIRECTORY", "").strip()
            if cred_dir:
                cand = Path(cred_dir) / "splitch_master_key"
                if cand.is_file():
                    key_file_env = str(cand)
        if key_file_env:
            kf = Path(key_file_env).expanduser()
            _assert_private_file_permissions(kf)
            return _derive_32b_key(kf.read_text(encoding="utf-8"))

        kr = _try_keyring_master_key()
        if kr is not None:
            return kr

        if self.key_path.is_file():
            _assert_private_file_permissions(self.key_path)
            return _derive_32b_key(self.key_path.read_text(encoding="utf-8"))

        if not create:
            raise FileNotFoundError(f"Vault master key not found at {self.key_path}")

        fresh = py_secrets.token_bytes(32)
        write_private_text(self.key_path, fresh.hex() + "\n")
        return fresh

    def _read_envelope(self) -> dict[str, Any]:
        if not self.vault_path.is_file():
            return {"version": 1, "alg": "AES-256-GCM", "entries": {}}
        try:
            raw = json.loads(self.vault_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
                return raw
        except (OSError, ValueError):
            pass
        return {"version": 1, "alg": "AES-256-GCM", "entries": {}}

    def _write_envelope(self, env: dict[str, Any]) -> None:
        write_private_text(self.vault_path, json.dumps(env, indent=2, sort_keys=True) + "\n")

    @staticmethod
    def _validate_name(name: str) -> str:
        cleaned = name.strip()
        if not _VAULT_NAME_RE.fullmatch(cleaned):
            raise ValueError(f"Invalid vault secret name: {name!r}")
        return cleaned

    def set(self, name: str, plaintext: str) -> str:
        clean_name = self._validate_name(name)
        with self._lock:
            key = self._resolve_master_key(create=True)
            nonce = py_secrets.token_bytes(12)
            aad = f"splitch-vault-v1:{clean_name}".encode()
            ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad)
            env = self._read_envelope()
            env["entries"][clean_name] = {
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ct": base64.b64encode(ct).decode("ascii"),
            }
            self._write_envelope(env)
        return f"vault:{clean_name}"

    def get(self, name: str) -> str | None:
        clean_name = self._validate_name(name)
        with self._lock:
            env = self._read_envelope()
            entry = env["entries"].get(clean_name)
            if not isinstance(entry, dict):
                return None
            try:
                key = self._resolve_master_key(create=False)
                nonce = base64.b64decode(entry["nonce"])
                ct = base64.b64decode(entry["ct"])
                aad = f"splitch-vault-v1:{clean_name}".encode()
                pt = AESGCM(key).decrypt(nonce, ct, aad)
                return pt.decode("utf-8")
            except (KeyError, ValueError, FileNotFoundError, InvalidTag, PermissionError):
                return None

    def delete(self, name: str) -> bool:
        clean_name = self._validate_name(name)
        with self._lock:
            env = self._read_envelope()
            if clean_name not in env["entries"]:
                return False
            del env["entries"][clean_name]
            self._write_envelope(env)
            return True

    def delete_prefix(self, prefix: str) -> int:
        with self._lock:
            env = self._read_envelope()
            to_del = [k for k in env["entries"] if k.startswith(prefix)]
            if not to_del:
                return 0
            for k in to_del:
                del env["entries"][k]
            self._write_envelope(env)
            return len(to_del)

    def list_names(self) -> list[str]:
        with self._lock:
            env = self._read_envelope()
            return sorted(str(k) for k in env["entries"])

    def has(self, name: str) -> bool:
        clean_name = self._validate_name(name)
        with self._lock:
            env = self._read_envelope()
            return clean_name in env["entries"]


# ── Secret Indirection Resolver (`env:`, `file:`, `vault:`) ──────────────────


def is_secret_reference(value: str) -> bool:
    v = value.strip()
    return (
        v.startswith(("env:", "file:", "vault:"))
        or bool(_ENV_BRACE_RE.fullmatch(v))
    )


def is_secret_env_key(key: str) -> bool:
    ku = key.strip().upper()
    if not ku:
        return False
    if ku.startswith(_SECRET_KEY_PREFIXES):
        return True
    return any(tok in ku for tok in _SECRET_KEY_SUBSTRINGS)


def resolve_secret_value(
    raw: str,
    *,
    vault: SecretVault | None = None,
    check_permissions: bool = True,
) -> str:
    """
    Resolve a secret reference (`env:VAR`, `${VAR}`, `file:/path`, `vault:name`) to its runtime value.
    Plain literal values are returned stripped unchanged.
    """
    val = (raw or "").strip()
    if not val:
        return ""

    if val.startswith("env:"):
        var_name = val[4:].strip()
        return os.getenv(var_name, "").strip()

    m_brace = _ENV_BRACE_RE.fullmatch(val)
    if m_brace:
        return os.getenv(m_brace.group(1), "").strip()

    if val.startswith("file:"):
        raw_path = val[5:].strip()
        raw_path = raw_path.removeprefix("//")
        expanded = os.path.expandvars(os.path.expanduser(raw_path))
        p = Path(expanded)
        if not p.is_file():
            raise FileNotFoundError(f"Referenced secret file not found: {p}")
        if check_permissions:
            _assert_private_file_permissions(p)
        return p.read_text(encoding="utf-8").strip()

    if val.startswith("vault:"):
        secret_name = val[6:].strip()
        if vault is None:
            return ""
        resolved = vault.get(secret_name)
        return resolved if resolved is not None else ""

    return val


def resolve_env_map(
    env_map: dict[str, str],
    *,
    vault: SecretVault | None = None,
    check_permissions: bool = True,
) -> dict[str, str]:
    """Resolve all `env:`, `file:`, and `vault:` pointers inside an MCP/process env dict."""
    out: dict[str, str] = {}
    for k, v in env_map.items():
        try:
            out[k] = resolve_secret_value(v, vault=vault, check_permissions=check_permissions)
        except (OSError, PermissionError, ValueError):
            out[k] = ""
    return out


# ── Asymmetric Ed25519 Router <-> Agent Signing ──────────────────────────────


def public_key_to_compact(pub: Ed25519PublicKey) -> str:
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return "ed25519:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_ed25519_keypair(
    priv_path: Path,
    pub_path: Path | None = None,
    *,
    force: bool = False,
) -> tuple[Path, str]:
    """Generate an Ed25519 keypair, saving PKCS8 private PEM (0600) and returning `(priv_path, compact_pub)`."""
    if priv_path.exists() and not force:
        priv = load_ed25519_private_key(str(priv_path))
        compact = public_key_to_compact(priv.public_key())
        if pub_path is not None and not pub_path.exists():
            pub_path.parent.mkdir(parents=True, exist_ok=True)
            pub_path.write_text(compact + "\n", encoding="utf-8")
        return priv_path, compact

    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    write_private_bytes(priv_path, priv_pem)
    compact = public_key_to_compact(priv.public_key())
    if pub_path is not None:
        pub_path.parent.mkdir(parents=True, exist_ok=True)
        pub_path.write_text(compact + "\n", encoding="utf-8")
    return priv_path, compact


def load_ed25519_private_key(raw_or_path: str) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from a `file:`/filesystem PEM path, inline PEM, or `ed25519:<b64>`."""
    cleaned = raw_or_path.strip()
    if cleaned.startswith("file:") or (
        "\n" not in cleaned
        and not cleaned.startswith(("-----BEGIN", "ed25519:"))
        and Path(os.path.expandvars(os.path.expanduser(cleaned))).is_file()
    ):
        p = Path(
            os.path.expandvars(
                os.path.expanduser(cleaned[5:].strip() if cleaned.startswith("file:") else cleaned)
            )
        )
        _assert_private_file_permissions(p)
        cleaned = p.read_text(encoding="utf-8").strip()

    if cleaned.startswith("ed25519:"):
        raw_bytes = _b64_decode_nopad(cleaned[8:].strip())
        return Ed25519PrivateKey.from_private_bytes(raw_bytes)

    key = serialization.load_pem_private_key(cleaned.encode("utf-8"), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("Loaded PEM key is not an Ed25519 private key")
    return key


def load_ed25519_public_key(raw_or_path: str) -> Ed25519PublicKey:
    """Load an Ed25519 public key from compact `ed25519:<b64>`, PEM text, or file path."""
    cleaned = raw_or_path.strip()
    if cleaned.startswith("file:"):
        p = Path(os.path.expandvars(os.path.expanduser(cleaned[5:].strip())))
        cleaned = p.read_text(encoding="utf-8").strip()
    elif (
        "\n" not in cleaned
        and not cleaned.startswith(("-----BEGIN", "ed25519:"))
        and Path(os.path.expandvars(os.path.expanduser(cleaned))).is_file()
    ):
        cleaned = Path(os.path.expandvars(os.path.expanduser(cleaned))).read_text(
            encoding="utf-8"
        ).strip()

    if cleaned.startswith("ed25519:"):
        raw_bytes = _b64_decode_nopad(cleaned[8:].strip())
        return Ed25519PublicKey.from_public_bytes(raw_bytes)

    key = serialization.load_pem_public_key(cleaned.encode("utf-8"))
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError("Loaded PEM key is not an Ed25519 public key")
    return key


def sign_agent_request_ed25519(
    priv: Ed25519PrivateKey,
    method: str,
    path: str,
    *,
    timestamp: int | None = None,
) -> dict[str, str]:
    """Produce short-lived Ed25519 request-authentication headers for router -> agent calls."""
    ts_str = str(int(time.time()) if timestamp is None else int(timestamp))
    msg = f"req:{ts_str}:{method.upper()}:{path}".encode()
    sig_b64 = base64.urlsafe_b64encode(priv.sign(msg)).decode("ascii").rstrip("=")
    return {
        "X-Splitch-Auth-Ts": ts_str,
        "X-Splitch-Auth-Sig": f"ed25519:{sig_b64}",
    }


def verify_agent_request_ed25519(
    pub: Ed25519PublicKey,
    ts_header: str,
    sig_header: str,
    method: str,
    path: str,
    *,
    max_age_s: int = 60,
    now_ts: float | None = None,
) -> bool:
    if not ts_header or not sig_header:
        return False
    sig_clean = sig_header.strip()
    if not sig_clean.startswith("ed25519:"):
        return False
    try:
        ts_val = int(ts_header.strip())
        sig_bytes = _b64_decode_nopad(sig_clean[8:])
    except ValueError:
        return False
    now = time.time() if now_ts is None else now_ts
    if abs(now - ts_val) > max_age_s:
        return False

    # Accept exact `(METHOD, path)` match, or wildcard `("ANY", "*")` for multi-request proxy clients
    for cand_m, cand_p in ((method.upper(), path), ("ANY", "*")):
        msg = f"req:{ts_val}:{cand_m}:{cand_p}".encode()
        try:
            pub.verify(sig_bytes, msg)
            return True
        except InvalidSignature:
            continue
    return False


def sign_tool_approval_ed25519(
    priv: Ed25519PrivateKey,
    tool: str,
    arguments: dict[str, Any],
    workspace_subpath: str = "",
    *,
    timestamp: int | None = None,
) -> tuple[str, str]:
    """Sign a router HITL tool-approval assertion with Ed25519 (agents verify with public key only)."""
    ts_str = str(int(time.time()) if timestamp is None else int(timestamp))
    msg = f"tool:{ts_str}:{_canonical_tool_payload(tool, arguments, workspace_subpath)}".encode()
    sig_b64 = base64.urlsafe_b64encode(priv.sign(msg)).decode("ascii").rstrip("=")
    return ts_str, f"ed25519:{sig_b64}"


def verify_tool_approval_ed25519(
    pub: Ed25519PublicKey,
    ts_header: str,
    sig_header: str,
    tool: str,
    arguments: dict[str, Any],
    workspace_subpath: str = "",
    *,
    max_age_s: int = 60,
    now_ts: float | None = None,
) -> bool:
    if not ts_header or not sig_header:
        return False
    sig_clean = sig_header.strip()
    if not sig_clean.startswith("ed25519:"):
        return False
    try:
        ts_val = int(ts_header.strip())
        sig_bytes = _b64_decode_nopad(sig_clean[8:])
    except ValueError:
        return False
    now = time.time() if now_ts is None else now_ts
    if abs(now - ts_val) > max_age_s:
        return False
    msg = f"tool:{ts_val}:{_canonical_tool_payload(tool, arguments, workspace_subpath)}".encode()
    try:
        pub.verify(sig_bytes, msg)
        return True
    except InvalidSignature:
        return False
