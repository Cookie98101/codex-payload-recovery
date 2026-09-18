from __future__ import annotations

import copy
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask


LOG = logging.getLogger("codexzh-payload-proxy")

TARGET_ORIGIN = os.getenv("CODEXZH_TARGET_ORIGIN", "https://api.codexzh.com").rstrip("/")
KEEP_RECENT_IMAGE_ITEMS = max(0, int(os.getenv("PRUNE_KEEP_RECENT_IMAGE_ITEMS", "2")))
MAX_BODY_BYTES = int(float(os.getenv("PRUNE_MAX_BODY_MB", "16")) * 1024 * 1024)

PLACEHOLDER = "[Historical image removed by the local payload proxy to prevent HTTP 413]"
TINY_TRANSPARENT_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+Xw7WAAAAAElFTkSuQmCC"
)
DATA_IMAGE_RE = re.compile(r"^data:image/[^;,]+;base64,", re.IGNORECASE)
BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@dataclass
class PruneStats:
    original_bytes: int
    final_bytes: int
    found_images: int = 0
    removed_images: int = 0
    removed_image_bytes: int = 0
    image_items: int = 0
    removed_orphan_outputs: int = 0


@dataclass
class ImageRef:
    top_index: int
    parent: Any
    key: Any
    original: Any
    estimated_bytes: int
    mode: str

    def remove(self) -> None:
        if self.mode == "content_item":
            self.parent[self.key] = {"type": "input_text", "text": PLACEHOLDER}
        elif self.mode == "image_url":
            self.parent[self.key] = TINY_TRANSPARENT_PNG
        elif self.mode == "top_level_generation":
            self.parent[self.key] = {
                "role": "user",
                "content": [{"type": "input_text", "text": PLACEHOLDER}],
            }


def _looks_like_raw_base64(value: Any) -> bool:
    if not isinstance(value, str) or len(value) < 4096:
        return False
    compact = "".join(value.split())
    return len(compact) % 4 == 0 and bool(BASE64_RE.fullmatch(compact))


def _estimate_image_bytes(value: str) -> int:
    if DATA_IMAGE_RE.match(value):
        value = value.split(",", 1)[1]
    return max(0, len(value) * 3 // 4)


def _collect_refs(node: Any, top_index: int, refs: list[ImageRef]) -> None:
    if isinstance(node, list):
        for index, child in enumerate(node):
            _collect_refs(child, top_index, refs)
        return

    if not isinstance(node, dict):
        return

    node_type = node.get("type")

    # Responses API message/tool content: replacing the complete content block
    # keeps the surrounding message or function output schema valid.
    image_url = node.get("image_url")
    if node_type == "input_image" and isinstance(image_url, str) and DATA_IMAGE_RE.match(image_url):
        refs.append(
            ImageRef(
                top_index=top_index,
                parent=None,
                key=None,
                original=node,
                estimated_bytes=_estimate_image_bytes(image_url),
                mode="content_item",
            )
        )
        return

    for key, value in node.items():
        if key == "image_url" and isinstance(value, str) and DATA_IMAGE_RE.match(value):
            refs.append(
                ImageRef(
                    top_index=top_index,
                    parent=node,
                    key=key,
                    original=value,
                    estimated_bytes=_estimate_image_bytes(value),
                    mode="image_url",
                )
            )
        else:
            _collect_refs(value, top_index, refs)


def _collect_image_refs(data: dict[str, Any]) -> list[ImageRef]:
    input_items = data.get("input")
    if not isinstance(input_items, list):
        return []

    refs: list[ImageRef] = []
    for top_index, item in enumerate(input_items):
        if isinstance(item, dict) and item.get("type") == "image_generation_call":
            result = item.get("result")
            if _looks_like_raw_base64(result):
                refs.append(
                    ImageRef(
                        top_index=top_index,
                        parent=input_items,
                        key=top_index,
                        original=item,
                        estimated_bytes=_estimate_image_bytes(result),
                        mode="top_level_generation",
                    )
                )
                continue

        before = len(refs)
        _collect_refs(item, top_index, refs)

        # Content-item replacements need their list parent. Resolve those here
        # in a second, small walk so generic fixed-schema image_url fields can
        # still use the tiny-image fallback above.
        if len(refs) > before:
            _bind_content_item_parents(item, refs[before:])
    return refs


def _bind_content_item_parents(node: Any, refs: list[ImageRef]) -> None:
    targets = {id(ref.original): ref for ref in refs if ref.mode == "content_item"}
    if not targets:
        return

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for index, child in enumerate(value):
                ref = targets.get(id(child))
                if ref is not None:
                    ref.parent = value
                    ref.key = index
                else:
                    walk(child)
        elif isinstance(value, dict):
            for child in value.values():
                walk(child)

    walk(node)


def _json_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _remove_orphan_tool_outputs(data: dict[str, Any]) -> int:
    input_items = data.get("input")
    if not isinstance(input_items, list):
        return 0

    call_ids: set[str] = set()
    for item in input_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"function_call", "custom_tool_call", "computer_call"}:
            call_id = item.get("call_id") or item.get("id")
            if isinstance(call_id, str):
                call_ids.add(call_id)

    cleaned_items = []
    removed = 0
    for item in input_items:
        if not isinstance(item, dict) or item.get("type") not in {
            "function_call_output",
            "custom_tool_call_output",
            "computer_call_output",
        }:
            cleaned_items.append(item)
            continue

        call_id = item.get("call_id")
        if call_id is None and item.get("type") == "function_call_output":
            call_id = item.get("id")

        if isinstance(call_id, str) and call_id in call_ids:
            cleaned_items.append(item)
        else:
            removed += 1

    if removed:
        data["input"] = cleaned_items
    return removed


def prune_responses_payload(
    body: bytes,
    keep_recent_image_items: int = KEEP_RECENT_IMAGE_ITEMS,
    max_body_bytes: int = MAX_BODY_BYTES,
) -> tuple[bytes, PruneStats]:
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("Responses request body must be a JSON object")

    data = copy.deepcopy(data)
    removed_orphan_outputs = _remove_orphan_tool_outputs(data)
    refs = _collect_image_refs(data)
    image_item_indexes = sorted({ref.top_index for ref in refs})
    keep_indexes = set(image_item_indexes[-keep_recent_image_items:]) if keep_recent_image_items else set()

    stats = PruneStats(
        original_bytes=len(body),
        final_bytes=len(body),
        found_images=len(refs),
        image_items=len(image_item_indexes),
        removed_orphan_outputs=removed_orphan_outputs,
    )

    removed_ids: set[int] = set()

    def remove(ref: ImageRef) -> None:
        if id(ref) in removed_ids:
            return
        ref.remove()
        removed_ids.add(id(ref))
        stats.removed_images += 1
        stats.removed_image_bytes += ref.estimated_bytes

    # Normal policy: keep only the latest N top-level history items containing
    # images. Top-level order matches the replay order sent by Codex.
    for ref in refs:
        if ref.top_index not in keep_indexes:
            remove(ref)

    encoded = _json_bytes(data)

    # Safety valve: if recent images alone still exceed the configured budget,
    # remove remaining images oldest-first until the request fits.
    if len(encoded) > max_body_bytes:
        for ref in refs:
            if id(ref) not in removed_ids:
                remove(ref)
                encoded = _json_bytes(data)
                if len(encoded) <= max_body_bytes:
                    break

    stats.final_bytes = len(encoded)
    return encoded, stats


def _request_headers(request: Request, body_length: int) -> list[tuple[str, str]]:
    headers = []
    for name, value in request.headers.items():
        lowered = name.lower()
        if lowered in HOP_BY_HOP_HEADERS or lowered in {"host", "content-length"}:
            continue
        headers.append((name, value))
    headers.append(("content-length", str(body_length)))
    return headers


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in HOP_BY_HOP_HEADERS and name.lower() != "content-length"
    }


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    timeout = httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0)
    app.state.client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    yield
    await app.state.client.aclose()


app = FastAPI(title="CodexZH Payload Proxy", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "target": TARGET_ORIGIN,
        "keep_recent_image_items": KEEP_RECENT_IMAGE_ITEMS,
        "max_body_bytes": MAX_BODY_BYTES,
    }


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request) -> Response:
    body = await request.body()
    is_responses_request = (
        request.method == "POST"
        and request.url.path.rstrip("/").endswith("/responses")
        and "application/json" in request.headers.get("content-type", "")
    )

    if is_responses_request and body:
        try:
            body, stats = prune_responses_payload(body)
        except (json.JSONDecodeError, ValueError) as exc:
            return JSONResponse(status_code=400, content={"error": f"Local proxy could not parse request: {exc}"})

        if stats.removed_images or stats.removed_orphan_outputs:
            LOG.info(
                "pruned %d/%d images and %d orphan tool outputs: %.2f MiB -> %.2f MiB "
                "(estimated image bytes removed: %.2f MiB)",
                stats.removed_images,
                stats.found_images,
                stats.removed_orphan_outputs,
                stats.original_bytes / 1024 / 1024,
                stats.final_bytes / 1024 / 1024,
                stats.removed_image_bytes / 1024 / 1024,
            )

        if len(body) > MAX_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "message": (
                            "The local proxy removed every inline image, but the remaining text/tool history "
                            f"is still {len(body) / 1024 / 1024:.2f} MiB. Compact this task once, then retry."
                        ),
                        "type": "local_payload_limit",
                    }
                },
            )

    upstream_url = f"{TARGET_ORIGIN}{request.url.path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    client: httpx.AsyncClient = request.app.state.client
    upstream_request = client.build_request(
        request.method,
        upstream_url,
        headers=_request_headers(request, len(body)),
        content=body,
    )

    try:
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.RequestError as exc:
        LOG.error("upstream connection failed: %r", exc)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "The local proxy could not reach CodexZH.", "type": "proxy_upstream_error"}},
        )

    return StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        headers=_response_headers(upstream_response),
        background=BackgroundTask(upstream_response.aclose),
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8787")), log_level="info")
