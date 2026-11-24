"""
Splitch Managed Apple Silicon MLX (`mlx-lm`) OpenAI-Compatible Server.

Executed inside the isolated `<data_root>/engines/mlx-lm/.venv` environment created by
`splitch engine install mlx-lm`. Provides:
  - `GET  /health`                 (identifies `engine: "mlx-lm"`)
  - `GET  /v1/models`              (lists local MLX repos + active loaded status)
  - `POST /models/load`            (pre-loads a local MLX model into Apple Unified Memory)
  - `POST /models/unload`          (drops active model + calls `mx.clear_cache()`)
  - `POST /v1/chat/completions`    (streaming & non-streaming generation)
  - `POST /v1/completions`         (streaming & non-streaming completion)
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def discover_mlx_models(models_dir: Path) -> dict[str, Path]:
    """Return `{model_id: model_path}` for valid MLX model directories under `models_dir`."""
    found: dict[str, Path] = {}
    if not models_dir.exists() or not models_dir.is_dir():
        return found
    for entry in sorted(models_dir.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / "config.json").is_file() and any(entry.glob("*.safetensors")):
            model_id = entry.name.replace("--", "/")
            found[model_id] = entry
    return found


class MlxModelManager:
    """Thread-safe single-active-model holder with explicit Apple Unified Memory eviction."""

    def __init__(self, models_dir: Path, kv_bits: int | None = None) -> None:
        self.models_dir = models_dir
        self.kv_bits = kv_bits
        self._lock = threading.Lock()
        self.active_model_id: str | None = None
        self.model: Any = None
        self.tokenizer: Any = None

    def list_models(self) -> list[dict[str, Any]]:
        discovered = discover_mlx_models(self.models_dir)
        out: list[dict[str, Any]] = []
        for mid in discovered:
            out.append(
                {
                    "id": mid,
                    "object": "model",
                    "owned_by": "mlx-lm",
                    "status": {"value": "loaded" if mid == self.active_model_id else "unloaded"},
                }
            )
        if self.active_model_id and self.active_model_id not in discovered:
            out.append(
                {
                    "id": self.active_model_id,
                    "object": "model",
                    "owned_by": "mlx-lm",
                    "status": {"value": "loaded"},
                }
            )
        return out

    def _resolve_model_ref(self, model_id: str) -> str:
        discovered = discover_mlx_models(self.models_dir)
        if model_id in discovered:
            return str(discovered[model_id])
        alt = model_id.replace("/", "--")
        candidate = self.models_dir / alt
        if candidate.is_dir() and (candidate / "config.json").is_file():
            return str(candidate)
        return model_id

    def load(self, model_id: str) -> None:
        import mlx.core as mx  # type: ignore[import-not-found]
        from mlx_lm import load as mlx_load  # type: ignore[import-not-found]

        with self._lock:
            if self.active_model_id == model_id and self.model is not None:
                return
            self._unload_unlocked(mx)
            ref = self._resolve_model_ref(model_id)
            model, tokenizer = mlx_load(ref)
            self.model = model
            self.tokenizer = tokenizer
            self.active_model_id = model_id

    def _unload_unlocked(self, mx: Any) -> None:
        self.model = None
        self.tokenizer = None
        self.active_model_id = None
        gc.collect()
        clear_fn = getattr(mx, "clear_cache", None)
        if callable(clear_fn):
            clear_fn()
        else:
            metal = getattr(mx, "metal", None)
            if metal is not None and callable(getattr(metal, "clear_cache", None)):
                metal.clear_cache()

    def unload(self, model_id: str | None = None) -> None:
        import mlx.core as mx  # type: ignore[import-not-found]

        with self._lock:
            if model_id and self.active_model_id and self.active_model_id != model_id:
                return
            self._unload_unlocked(mx)

    def _format_prompt(self, messages: list[dict[str, Any]]) -> str:
        tok = self.tokenizer
        if tok is not None and hasattr(tok, "apply_chat_template"):
            try:
                return str(
                    tok.apply_chat_template(
                        messages, tokenize=False, add_generation_prompt=True
                    )
                )
            except (TypeError, ValueError):
                pass
        parts: list[str] = []
        for m in messages:
            role = str(m.get("role") or "user")
            content = m.get("content") or ""
            if isinstance(content, list):
                content = "\n".join(
                    str(c.get("text") or "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                )
            parts.append(f"{role}: {content}")
        parts.append("assistant:")
        return "\n".join(parts)

    def generate_stream(
        self,
        model_id: str,
        prompt: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        top_p: float = 0.95,
    ):
        from mlx_lm import stream_generate  # type: ignore[import-not-found]
        from mlx_lm.sample_utils import make_sampler  # type: ignore[import-not-found]

        self.load(model_id)
        sampler = make_sampler(temp=temperature, top_p=top_p)
        kwargs: dict[str, Any] = {
            "max_tokens": max_tokens,
            "sampler": sampler,
        }
        if self.kv_bits in (4, 8):
            kwargs["kv_bits"] = self.kv_bits

        with self._lock:
            if self.model is None or self.tokenizer is None:
                raise RuntimeError("MLX model was unloaded during generation")
            yield from stream_generate(self.model, self.tokenizer, prompt, **kwargs)


def _make_handler(manager: MlxModelManager):
    class MlxHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            sys.stderr.write(f"[splitch-mlx] {format % args}\n")

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(body.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "engine": "mlx-lm",
                        "active_model": manager.active_model_id,
                    },
                )
                return
            if path in ("/v1/models", "/models"):
                self._send_json(200, {"object": "list", "data": manager.list_models()})
                return
            self._send_json(404, {"error": f"Not found: {path}"})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            try:
                body = self._read_json()
            except (ValueError, UnicodeDecodeError) as exc:
                self._send_json(400, {"error": f"Invalid JSON: {exc}"})
                return

            if path == "/models/load":
                mid = str(body.get("model") or "").strip()
                if not mid:
                    self._send_json(400, {"error": "Missing 'model'"})
                    return
                try:
                    manager.load(mid)
                    self._send_json(200, {"ok": True, "action": "load", "model": mid})
                except Exception as exc:  # noqa: BLE001
                    self._send_json(500, {"error": str(exc)})
                return

            if path == "/models/unload":
                mid = str(body.get("model") or "").strip() or None
                try:
                    manager.unload(mid)
                    self._send_json(200, {"ok": True, "action": "unload", "model": mid or ""})
                except Exception as exc:  # noqa: BLE001
                    self._send_json(500, {"error": str(exc)})
                return

            if path in ("/v1/chat/completions", "/v1/completions"):
                is_chat = path == "/v1/chat/completions"
                model_id = str(body.get("model") or manager.active_model_id or "").strip()
                if not model_id:
                    models = manager.list_models()
                    if models:
                        model_id = str(models[0]["id"])
                if not model_id:
                    self._send_json(400, {"error": "No model specified and no local MLX models found"})
                    return

                stream = bool(body.get("stream", False))
                max_tokens = int(body.get("max_tokens") or body.get("max_completion_tokens") or 1024)
                temperature = float(body.get("temperature", 0.7))
                top_p = float(body.get("top_p", 0.95))

                try:
                    manager.load(model_id)
                    prompt = (
                        manager._format_prompt(body.get("messages") or [])
                        if is_chat
                        else str(body.get("prompt") or "")
                    )
                except Exception as exc:  # noqa: BLE001
                    self._send_json(500, {"error": str(exc)})
                    return

                req_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                created = int(time.time())

                if stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        for chunk in manager.generate_stream(
                            model_id,
                            prompt,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            top_p=top_p,
                        ):
                            text = str(getattr(chunk, "text", ""))
                            if not text:
                                continue
                            ev = (
                                {
                                    "id": req_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": model_id,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"content": text},
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                                if is_chat
                                else {
                                    "id": req_id,
                                    "object": "text_completion",
                                    "created": created,
                                    "model": model_id,
                                    "choices": [{"index": 0, "text": text, "finish_reason": None}],
                                }
                            )
                            self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                            self.wfile.flush()
                        done_ev = {
                            "id": req_id,
                            "object": "chat.completion.chunk" if is_chat else "text_completion",
                            "created": created,
                            "model": model_id,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {} if is_chat else None,
                                    "text": "" if not is_chat else None,
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                        self.wfile.write(f"data: {json.dumps(done_ev)}\n\n".encode())
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    return

                parts: list[str] = []
                prompt_tokens = 0
                gen_tokens = 0
                try:
                    for chunk in manager.generate_stream(
                        model_id,
                        prompt,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        top_p=top_p,
                    ):
                        parts.append(str(getattr(chunk, "text", "")))
                        prompt_tokens = int(getattr(chunk, "prompt_tokens", prompt_tokens) or 0)
                        gen_tokens = int(getattr(chunk, "generation_tokens", gen_tokens) or 0)
                except Exception as exc:  # noqa: BLE001
                    self._send_json(500, {"error": str(exc)})
                    return

                full_text = "".join(parts)
                usage = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": gen_tokens,
                    "total_tokens": prompt_tokens + gen_tokens,
                }
                if is_chat:
                    self._send_json(
                        200,
                        {
                            "id": req_id,
                            "object": "chat.completion",
                            "created": created,
                            "model": model_id,
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": full_text},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": usage,
                        },
                    )
                else:
                    self._send_json(
                        200,
                        {
                            "id": req_id,
                            "object": "text_completion",
                            "created": created,
                            "model": model_id,
                            "choices": [{"index": 0, "text": full_text, "finish_reason": "stop"}],
                            "usage": usage,
                        },
                    )
                return

            self._send_json(404, {"error": f"Not found: {path}"})

    return MlxHandler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Splitch managed MLX-LM OpenAI server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--models-dir", default=str(Path.home() / "models" / "mlx"))
    parser.add_argument("--kv-bits", type=int, choices=[4, 8], default=None)
    args = parser.parse_args(argv)

    models_dir = Path(args.models_dir).expanduser()
    models_dir.mkdir(parents=True, exist_ok=True)
    manager = MlxModelManager(models_dir, kv_bits=args.kv_bits)
    server = ThreadingHTTPServer((args.host, args.port), _make_handler(manager))
    print(f"[splitch-mlx] Serving MLX models from {models_dir} on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
