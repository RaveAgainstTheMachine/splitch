"""
Standalone Jeveration inference worker (runs inside the isolated Python 3.12 CUDA venv).

Provides single-forward-pass 512x512 Text-to-Image, Image-to-Image (hed, depth, colorize,
anime, ghibli, watercolor), and Dense Prediction (segmentation, depth, surface normals).
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field


class T2IRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)
    seed: int = 0


class I2IRequest(BaseModel):
    image_b64: str = Field(..., min_length=1)
    task: str = Field(..., min_length=1)
    caption: str = ""
    from_photo: bool = False
    seed: int = 0


class DenseRequest(BaseModel):
    image_b64: str = Field(..., min_length=1)


def _decode_image_bytes(raw_b64: str) -> bytes:
    payload = raw_b64.strip()
    if payload.startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1]
    return base64.b64decode(payload)


def _array_to_png_bytes(arr: Any) -> bytes:
    from PIL import Image  # type: ignore[import-not-found]

    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def create_worker_app(
    *,
    repo_dir: Path,
    weights_dir: Path,
    load_source_decoder: bool = True,
) -> FastAPI:
    if str(repo_dir) not in sys.path:
        sys.path.insert(0, str(repo_dir))

    worker_app = FastAPI(title="Splitch Jeveration Worker")
    holder: dict[str, Any] = {"pipe": None, "error": None}

    @worker_app.on_event("startup")
    def _load_pipeline() -> None:
        try:
            from jeveration import JeverationPipeline  # type: ignore[import-not-found]

            weights_pt = weights_dir / "jeveration.pt"
            dec_pt = weights_dir / "decoder512.pt"
            src_dec_pt = weights_dir / "source_decoder512.pt"
            kwargs: dict[str, Any] = {"decoder": str(dec_pt)}
            if load_source_decoder and src_dec_pt.is_file():
                kwargs["source_decoder"] = str(src_dec_pt)
            holder["pipe"] = JeverationPipeline(str(weights_pt), **kwargs)
        except Exception as exc:  # noqa: BLE001
            holder["error"] = str(exc)

    @worker_app.get("/health")
    def health() -> dict[str, Any]:
        import torch  # type: ignore[import-not-found]

        pipe = holder["pipe"]
        vram_mb = 0.0
        if torch.cuda.is_available():
            vram_mb = round(torch.cuda.memory_allocated() / (1024 * 1024), 1)
        return {
            "status": "ok" if pipe is not None else "error",
            "ready": pipe is not None,
            "error": holder["error"],
            "device": "cuda" if torch.cuda.is_available() else "cpu",
            "vram_mb": vram_mb,
        }

    @worker_app.post("/v1/text-to-image")
    def text_to_image(req: T2IRequest) -> Response:
        pipe = holder["pipe"]
        if pipe is None:
            raise HTTPException(status_code=503, detail=holder["error"] or "Pipeline not ready")
        t0 = time.perf_counter()
        images = pipe.text_to_image([req.prompt], pipe.generator(req.seed))
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        png = _array_to_png_bytes(images[0])
        return Response(
            content=png,
            media_type="image/png",
            headers={"X-Jeveration-Latency-Ms": str(latency_ms)},
        )

    @worker_app.post("/v1/image-to-image")
    def image_to_image(req: I2IRequest) -> Response:
        import numpy as np  # type: ignore[import-not-found]
        from jeveration.images import load_square, to_gray  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]

        pipe = holder["pipe"]
        if pipe is None:
            raise HTTPException(status_code=503, detail=holder["error"] or "Pipeline not ready")

        raw_img = _decode_image_bytes(req.image_b64)
        pil_img = Image.open(io.BytesIO(raw_img)).convert("RGB")
        # Save/load via load_square semantics (center-crop & resize 512x512)
        side = min(pil_img.size)
        left = (pil_img.width - side) // 2
        top = (pil_img.height - side) // 2
        sq = pil_img.crop((left, top, left + side, top + side)).resize((512, 512))
        arr = np.asarray(sq)
        if req.task == "colorize":
            arr = to_gray(arr)
        elif callable(load_square) and arr.dtype != np.uint8:
            arr = arr.astype(np.uint8)

        t0 = time.perf_counter()
        if req.task in ("hed", "depth"):
            cap = req.caption.strip() or "a high quality photograph"
            kwargs: dict[str, Any] = {"generator": pipe.generator(req.seed)}
            if req.from_photo:
                kwargs["from_photo"] = True
            images = pipe.image_to_image(arr[None], req.task, [cap], **kwargs)
        else:
            images = pipe.image_to_image(arr[None], req.task, generator=pipe.generator(req.seed))
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        png = _array_to_png_bytes(images[0])
        return Response(
            content=png,
            media_type="image/png",
            headers={"X-Jeveration-Latency-Ms": str(latency_ms)},
        )

    @worker_app.post("/v1/dense-predict")
    def dense_predict(req: DenseRequest) -> dict[str, Any]:
        import numpy as np  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]

        pipe = holder["pipe"]
        if pipe is None:
            raise HTTPException(status_code=503, detail=holder["error"] or "Pipeline not ready")

        raw_img = _decode_image_bytes(req.image_b64)
        pil_img = Image.open(io.BytesIO(raw_img)).convert("RGB")
        side = min(pil_img.size)
        left = (pil_img.width - side) // 2
        top = (pil_img.height - side) // 2
        sq = pil_img.crop((left, top, left + side, top + side)).resize((512, 512))
        arr = np.asarray(sq)

        t0 = time.perf_counter()
        pred = pipe.dense_predict(arr[None])
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        seg = pred["seg"][0]
        # Deterministic color palette for segmentation class indices
        rng = np.random.default_rng(42)
        palette = rng.integers(32, 245, size=(256, 3), dtype=np.uint8)
        palette[0] = np.array([18, 20, 30], dtype=np.uint8)
        seg_rgb = palette[np.clip(seg.astype(np.int64), 0, 255)]

        depth = pred["depth"][0].astype(np.float32)
        d_min = float(np.nanmin(depth))
        d_max = float(np.nanmax(depth))
        span = max(1e-5, d_max - d_min)
        depth_norm = np.clip((depth - d_min) / span, 0.0, 1.0)
        depth_u8 = (255.0 * (1.0 - depth_norm)).astype(np.uint8)
        depth_rgb = np.stack([depth_u8, depth_u8, depth_u8], axis=-1)

        normal = pred["normal"][0].astype(np.float32)
        normal_u8 = np.clip(((normal + 1.0) * 0.5) * 255.0, 0.0, 255.0).astype(np.uint8)

        return {
            "seg_png_b64": base64.b64encode(_array_to_png_bytes(seg_rgb)).decode("ascii"),
            "depth_png_b64": base64.b64encode(_array_to_png_bytes(depth_rgb)).decode("ascii"),
            "normal_png_b64": base64.b64encode(_array_to_png_bytes(normal_u8)).decode("ascii"),
            "depth_min_m": round(d_min, 3),
            "depth_max_m": round(d_max, 3),
            "latency_ms": latency_ms,
        }

    return worker_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Splitch Jeveration Worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8084)
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--weights-dir", required=True)
    parser.add_argument("--hf-home", default="")
    parser.add_argument("--no-source-decoder", action="store_true")
    args = parser.parse_args()

    if args.hf_home:
        os.environ["HF_HOME"] = args.hf_home

    import uvicorn

    worker_app = create_worker_app(
        repo_dir=Path(args.repo_dir),
        weights_dir=Path(args.weights_dir),
        load_source_decoder=not args.no_source_decoder,
    )
    uvicorn.run(worker_app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
