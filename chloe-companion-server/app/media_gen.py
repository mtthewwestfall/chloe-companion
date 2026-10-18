"""Background Sogni generation for REGULAR media requests.

A request row is created as `pending`; the endpoint spawns a daemon thread
running `run_generation(request_id)` here. The worker opens its OWN sqlite
connection (connections are not shared across threads), submits the job to
Sogni over websocket (job submission is socket-only), waits for completion,
downloads the result, saves it under config.GENERATED_DIR, and flips the row
to `ready`. Any failure flips it to `failed` and refunds the consumed pool
unit via store.refund_request().

Requires the SOGNI_API_KEY Railway env var (raw key). Nothing here runs
without it — the endpoint refuses with 503 before consuming balance.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import traceback
import urllib.request
from typing import Any

from . import config, db, gemini_keys, store

_WS_PATCHED = False


def _patch_websocket_quirk() -> None:
    """Sogni's uWebSockets server answers the 101 upgrade with
    `Connection: close` instead of `Connection: Upgrade`, which the strict
    websockets client rejects. Rewrite the header before validation (same
    patch the operator CLI uses)."""
    global _WS_PATCHED
    if _WS_PATCHED:
        return
    try:
        from websockets.asyncio import client as _ws_async_client
        from websockets.asyncio.client import connect as _ws_connect  # noqa: F401
        from websockets.client import ClientProtocol as _BaseProtocol
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        class _SogniClientProtocol(_BaseProtocol):
            def process_response(self, response):
                if response.status_code == 101:
                    headers = response.headers
                    conn_vals = headers.get_all("Connection")
                    upg_vals = headers.get_all("Upgrade")
                    if any(v.lower() == "websocket" for v in upg_vals) and not any(
                        "upgrade" in v.lower() for v in conn_vals
                    ):
                        raw = [
                            (k, "Upgrade" if k.lower() == "connection" else v)
                            for k, v in headers.raw_items()
                        ]
                        response = Response(
                            response.status_code,
                            response.reason_phrase,
                            Headers(raw),
                            response.body,
                        )
                return super().process_response(response)

        _ws_async_client.ClientProtocol = _SogniClientProtocol
    except Exception as e:  # noqa: BLE001 — without the patch, Sogni fails fast
        print(f"[media_gen] websocket quirk patch unavailable: {e}", flush=True)
    _WS_PATCHED = True


def _ws_factory(url, **kwargs):
    from websockets.asyncio.client import connect as ws_connect

    return ws_connect(url, **kwargs)


def _download(url: str, dest: str, timeout: int = 300) -> None:
    """Download with retries (Sogni's S3 streams flake mid-transfer)."""
    last: Exception | None = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp, \
                    open(dest, "wb") as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            if os.path.getsize(dest) > 1024:
                return
            last = RuntimeError("downloaded file too small")
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"[media_gen] download attempt {attempt + 1} failed: {e}",
                  flush=True)
    raise RuntimeError(f"download failed: {last}")


def _local_media_file(url: str) -> str | None:
    """On-disk file for an app-relative media URL this process serves
    (/generated/... lives in config.GENERATED_DIR, /media/... in the
    PWA media dir — the same mapping main uses to serve them), or None
    when the URL is not one of ours or the file is not on this disk.

    The returned path is the SERVED library file itself: callers must
    copy it, never mutate or delete it."""
    if not url or not url.startswith("/"):
        return None
    rel = url.split("?", 1)[0]
    if rel.startswith("/generated/"):
        base = config.GENERATED_DIR
        name = rel[len("/generated/"):]
    elif rel.startswith("/media/"):
        base = os.path.join(os.path.dirname(config.DATA_DIR),
                            "chloe-companion-app", "media")
        name = rel[len("/media/"):]
    else:
        return None
    if not name:
        return None
    base = os.path.abspath(base)
    path = os.path.abspath(os.path.join(base, name))
    if os.path.commonpath([base, path]) != base:
        return None  # escapes the served dir — not ours to hand out
    return path if os.path.isfile(path) else None


def _fetch_reference(avatar_url: str) -> str | None:
    """Download the girl's avatar as the face-lock reference frame."""
    if not avatar_url:
        return None
    url = avatar_url
    if url.startswith("/"):
        # App-relative media URL (her girl_media rows carry these —
        # e.g. owner uploads at /generated/<file>): urllib cannot open
        # a scheme-less URL at all ("unknown url type"), so resolve it
        # here instead of handing the raw path to _download. The file
        # is one this process serves: prefer the on-disk file, COPIED
        # to a temp file (callers delete the path we return — the
        # library original must survive), and fall back to fetching
        # it from the app's public base when it is not on this disk.
        local = _local_media_file(url)
        if local:
            try:
                fd, path = tempfile.mkstemp(suffix=".ref")
                with os.fdopen(fd, "wb") as out, \
                        open(local, "rb") as src:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                if os.path.getsize(path) > 1024:
                    return path
                os.unlink(path)  # stub-sized: fall through to download
            except OSError as e:  # noqa: BLE001
                print(f"[media_gen] local reference copy failed: {e}",
                      flush=True)
        url = config.PUBLIC_APP_URL + url
    try:
        fd, path = tempfile.mkstemp(suffix=".ref")
        os.close(fd)
        _download(url, path, timeout=60)
        return path
    except Exception as e:  # noqa: BLE001
        print(f"[media_gen] reference download failed: {e}", flush=True)
        return None


def _build_prompt(girl_name: str, user_prompt: str, media: str,
                  gift_fragment: str = "") -> str:
    base = (f"Photorealistic {girl_name}, {user_prompt.strip()}"
            if user_prompt.strip() else f"Photorealistic portrait of {girl_name}")
    if gift_fragment:
        base = f"{base}, {gift_fragment}"
    tail = ("clean high-quality footage, natural anatomy, soft flattering "
            "light, coherent background")
    if media == "video":
        tail += ", subtle natural motion, she looks at the camera"
    return f"{base}, {tail}"


async def _generate_async(request_id: int, girl: dict[str, Any], media: str,
                          prompt: str,
                          gift_fragment: str = "") -> tuple[str, float]:
    """Run the Sogni job. Returns (saved_relative_url, cost_usd)."""
    from sogni_client import SogniClient

    _patch_websocket_quirk()
    ref_path = _fetch_reference(girl.get("avatar_url") or "")
    params: dict[str, Any] = {
        "positivePrompt": _build_prompt(girl.get("name") or "Chloe", prompt, media, gift_fragment),
        "numberOfMedia": 1,
    }
    if ref_path:
        params["referenceImage"] = ref_path
    try:
        if media == "video":
            params.update({
                "type": "video",
                "modelId": config.SOGNI_VIDEO_MODEL,
                # H3 models reject negativePrompt — fold quality into the prompt.
                "duration": 6,
                "fps": 24,
                "width": 704,
                "height": 1248,
            })
        else:
            params.update({
                "type": "image",
                "modelId": config.SOGNI_IMAGE_MODEL,
                "width": 768,
                "height": 1344,
            })
        client = await SogniClient.create(
            api_key=config.SOGNI_API_KEY,
            app_id="companion-media-requests",
            disable_socket=False,
            websocket_factory=_ws_factory,
        )
        try:
            project = await client.projects.create(params)
            urls = await project.wait_for_completion(timeout=600)
            if not urls:
                # Mint fresh S3 URLs from the finished worker jobs.
                info = await client.projects.get(project.id)
                for wj in (info.get("completedWorkerJobs") or []):
                    img_id = wj.get("imgID") or wj.get("id")
                    if not img_id:
                        continue
                    try:
                        u = await client.projects.media_download_url(
                            {"jobId": project.id, "id": img_id,
                             "type": "complete"})
                        urls.append(u)
                    except Exception as e:  # noqa: BLE001
                        print(f"[media_gen] mint failed for {img_id}: {e}",
                              flush=True)
            if not urls:
                raise RuntimeError("no result urls from Sogni")
            ext = ".mp4" if media == "video" else ".jpg"
            os.makedirs(config.GENERATED_DIR, exist_ok=True)
            dest = os.path.join(config.GENERATED_DIR, f"req_{request_id}{ext}")
            _download(urls[0], dest)
            spark = 0.0
            try:
                info = await client.projects.get(project.id)
                spark = float((info.get("costActual") or {}).get("costInSpark") or 0)
            except Exception:  # noqa: BLE001
                pass
            cost_usd = spark * 0.005 if spark else config.MEDIA_REQUEST_COST_USD[
                f"regular_{media}"]
            return f"/generated/req_{request_id}{ext}", cost_usd
        finally:
            await client.aclose()
    finally:
        if ref_path:
            try:
                os.unlink(ref_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Face-match gate (2026-10-17): a generated photo is only delivered when
# it actually looks like the character. The avatar is the reference (the
# same one used as the generation face-lock). An explicit "no" from the
# vision check retries the generation once; a second "no" fails the
# request and refunds the pool unit instead of delivering a stranger.
# "uncertain"/unavailable (no reference, vision down) never blocks —
# availability beats perfection.
# ---------------------------------------------------------------------------
_FACE_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.1-flash-lite:generateContent"
)
_FACE_CHECK_PROMPT = (
    "You are verifying identity between two photos. The FIRST image is "
    "the reference portrait of a woman. The SECOND image is a newly "
    "generated photo that should depict the SAME woman. Compare facial "
    "features, face shape, eyes, nose, lips and hair — ignore clothing, "
    "background, lighting and pose. Respond with ONLY this JSON: "
    "{\"same_person\": \"yes\"} if it is clearly the same person, "
    "{\"same_person\": \"no\"} if it is clearly a different person, or "
    "{\"same_person\": \"uncertain\"} if the second image has no "
    "clearly visible face."
)
_FACE_MAX_BYTES = 4_000_000


def _reference_bytes(avatar_url: str) -> tuple[bytes, str] | None:
    """(bytes, mime) for the girl's avatar — http(s), app-relative, or
    data URI (Brittany's avatar is a data URI, which _fetch_reference
    cannot use for generation but the check can read directly)."""
    url = (avatar_url or "").strip()
    if not url:
        return None
    try:
        if url.startswith("data:image/"):
            head, _, b64 = url.partition(",")
            mime = head[5:].split(";")[0] or "image/jpeg"
            import base64 as _b64
            raw = _b64.b64decode(b64)
            return (raw, mime) if 0 < len(raw) <= _FACE_MAX_BYTES else None
        if url.startswith("/"):
            path = _local_media_file(url)
            if not path:
                return None
            with open(path, "rb") as f:
                raw = f.read(_FACE_MAX_BYTES + 1)
            if not raw or len(raw) > _FACE_MAX_BYTES:
                return None
            ext = os.path.splitext(path)[1].lower()
            mime = {".png": "image/png", ".webp": "image/webp"}.get(
                ext, "image/jpeg")
            return raw, mime
        if url.startswith("http://") or url.startswith("https://"):
            req = urllib.request.Request(
                url, headers={"User-Agent": "chloe-companion/facecheck"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read(_FACE_MAX_BYTES + 1)
                mime = resp.headers.get_content_type() or "image/jpeg"
            if not raw or len(raw) > _FACE_MAX_BYTES:
                return None
            return raw, mime
    except Exception as e:  # noqa: BLE001
        print(f"[media_gen] reference read failed: {e.__class__.__name__}",
              flush=True)
    return None


def _face_verdict(girl: dict[str, Any], result_url: str) -> str:
    """'yes' | 'no' | 'uncertain' | 'unavailable'. Never raises."""
    try:
        ref = _reference_bytes(girl.get("avatar_url") or "")
        if not ref:
            return "unavailable"
        gen_path = _local_media_file(result_url)
        if not gen_path:
            return "unavailable"
        with open(gen_path, "rb") as f:
            gen_raw = f.read(_FACE_MAX_BYTES + 1)
        if not gen_raw or len(gen_raw) > _FACE_MAX_BYTES:
            return "unavailable"
        import base64 as _b64
        parts = [
            {"text": _FACE_CHECK_PROMPT},
            {"inline_data": {"mime_type": ref[1],
                             "data": _b64.b64encode(ref[0]).decode()}},
            {"inline_data": {"mime_type": "image/jpeg",
                             "data": _b64.b64encode(gen_raw).decode()}},
        ]
        data = gemini_keys.post_json(
            _FACE_GEMINI_URL,
            {"contents": [{"role": "user", "parts": parts}],
             "generationConfig": {"temperature": 0.0, "maxOutputTokens": 100}},
            timeout=45, source="whispr-facecheck")
        if not data:
            return "unavailable"
        text = "".join(p.get("text", "") for p in
                       data["candidates"][0]["content"]["parts"]).strip()
        if text.startswith("```"):
            import re as _re
            text = _re.sub(r"^```\w*\n?", "", text)
            text = _re.sub(r"\n?```$", "", text)
        import json as _json
        verdict = str(_json.loads(text).get("same_person", "")).lower()
        return verdict if verdict in ("yes", "no", "uncertain") else "unavailable"
    except Exception as e:  # noqa: BLE001
        print(f"[media_gen] face check unavailable: {e.__class__.__name__}",
              flush=True)
        return "unavailable"


def run_generation(request_id: int) -> None:
    """Thread entry: fulfill one regular request, or fail + refund it."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM media_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if not row or row["status"] != "pending":
            return
        req = dict(row)
        # Resolve the girl for face-lock / naming. Import here to avoid a
        # hard import cycle (characters imports config only, but be safe).
        from . import characters
        girl = characters.get_character(req["girl_id"])
        _gift_frag = ""
        try:
            _gift_frag = store.gift_visual_fragment(
                conn, req["user_id"], req["girl_id"])
        except Exception:
            _gift_frag = ""
        try:
            url, cost = asyncio.run(
                _generate_async(request_id, girl, req["media"], req["prompt"],
                                gift_fragment=_gift_frag))
        except Exception as e:  # noqa: BLE001
            print(f"[media_gen] request {request_id} failed: {e}\n"
                  f"{traceback.format_exc()}", flush=True)
            store.set_media_request_status(conn, request_id, "failed")
            store.refund_request(conn, req["user_id"], req["pool_source"])
            return
        if req["media"] == "image":
            verdict = _face_verdict(girl, url)
            print(f"[media_gen] request {request_id} face check: {verdict}",
                  flush=True)
            if verdict == "no":
                # One retry with a fresh generation; a second clear
                # mismatch fails + refunds rather than delivering a
                # photo of the wrong woman.
                try:
                    url, cost = asyncio.run(_generate_async(
                        request_id, girl, req["media"], req["prompt"],
                        gift_fragment=_gift_frag))
                    verdict = _face_verdict(girl, url)
                    print(f"[media_gen] request {request_id} face check "
                          f"(retry): {verdict}", flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"[media_gen] request {request_id} retry failed: "
                          f"{e}", flush=True)
                    verdict = "no"
                if verdict == "no":
                    store.set_media_request_status(conn, request_id, "failed")
                    store.refund_request(conn, req["user_id"],
                                         req["pool_source"])
                    return
        store.set_media_request_status(conn, request_id, "ready",
                                       result_url=url, cost_usd=cost)
        print(f"[media_gen] request {request_id} ready: {url} "
              f"(${cost:.4f})", flush=True)
    finally:
        conn.close()


def spawn_generation(request_id: int) -> None:
    t = threading.Thread(target=run_generation, args=(request_id,),
                         daemon=True, name=f"media-gen-{request_id}")
    t.start()
