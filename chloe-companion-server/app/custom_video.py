"""Background Sogni generation for CUSTOM video requests.

A request row is created as `pending`; the endpoint spawns a daemon thread
running `run_custom_generation(request_id)` here. The worker opens its OWN
sqlite connection (connections are not shared across threads), submits the
job to Sogni over websocket (job submission is socket-only), waits for
completion, downloads the result, saves it under config.GENERATED_DIR,
registers it in `girl_media` (kind='give', preset='custom') so it joins her
give library, and flips the request row to `fulfilled`.

Sogni content blocks ("Sensitive content detected") and any other failure
leave the row as `pending` — the owner fulfills those manually. Nothing is
refunded because custom requests cost the user nothing up front.

Requires the SOGNI_API_KEY Railway env var (raw key). Without it the
endpoint still accepts requests but they stay `pending` for manual
fulfillment.
"""
from __future__ import annotations

import asyncio
import os
import threading
import traceback
import urllib.request
from typing import Any

from . import config, db, store
from .media_gen import (
    _download,
    _fetch_reference,
    _patch_websocket_quirk,
    _ws_factory,
)


def _build_custom_prompt(girl_name: str, description: str) -> str:
    desc = (description or "").strip()[:1000] or "smiling at the camera"
    tail = ("clean high-quality footage, natural anatomy, soft flattering "
            "light, coherent background, subtle natural motion, "
            "she looks at the camera")
    return f"Photorealistic {girl_name}, {desc}, {tail}"


async def _generate_custom_async(request_id: int, girl: dict[str, Any],
                                 description: str) -> tuple[str, float]:
    """Run the Sogni job. Returns (saved_relative_url, cost_usd)."""
    from sogni_client import SogniClient

    _patch_websocket_quirk()
    ref_path = _fetch_reference(girl.get("avatar_url") or "")
    params: dict[str, Any] = {
        "positivePrompt": _build_custom_prompt(girl.get("name") or "Chloe",
                                               description),
        "numberOfMedia": 1,
        "type": "video",
        "modelId": config.SOGNI_VIDEO_MODEL,
        # H3 models reject negativePrompt — fold quality into the prompt.
        "duration": 6,
        "fps": 24,
        "width": 704,
        "height": 1248,
    }
    if ref_path:
        params["referenceImage"] = ref_path
    try:
        client = await SogniClient.create(
            api_key=config.SOGNI_API_KEY,
            app_id="companion-custom-videos",
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
                        print(f"[custom_video] mint failed for {img_id}: {e}",
                              flush=True)
            if not urls:
                raise RuntimeError("no result urls from Sogni")
            os.makedirs(config.GENERATED_DIR, exist_ok=True)
            dest = os.path.join(config.GENERATED_DIR, f"custom_{request_id}.mp4")
            _download(urls[0], dest)
            spark = 0.0
            try:
                info = await client.projects.get(project.id)
                spark = float((info.get("costActual") or {}).get("costInSpark") or 0)
            except Exception:  # noqa: BLE001
                pass
            cost_usd = spark * 0.005 if spark else 0.12
            return f"/generated/custom_{request_id}.mp4", cost_usd
        finally:
            await client.aclose()
    finally:
        if ref_path:
            try:
                os.unlink(ref_path)
            except OSError:
                pass


def run_custom_generation(request_id: int) -> None:
    """Thread entry: try Sogni; on success register the clip, on any
    failure leave the request `pending` for manual fulfillment."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM custom_video_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if not row or row["status"] != "pending":
            return
        req = dict(row)

        cur = conn.execute(
            "UPDATE custom_video_requests SET status = 'generating' "
            "WHERE id = ? AND status = 'pending'",
            (request_id,),
        )
        conn.commit()
        if cur.rowcount != 1:
            return

        # Resolve the girl for face-lock / naming. Import here to avoid a
        # hard import cycle (characters imports config only, but be safe).
        from . import characters
        girl = characters.get_character(req["girl_id"], user_id=req["user_id"])
        if not girl:
            store.update_custom_request(conn, request_id, "pending")
            return

        try:
            url, cost = asyncio.run(
                _generate_custom_async(request_id, girl, req["description"]))
        except Exception as e:  # noqa: BLE001
            # Sogni block ("Sensitive content detected") or any other
            # failure: back to pending so the owner can fulfill manually.
            print(f"[custom_video] request {request_id} not auto-fulfilled: {e}\n"
                  f"{traceback.format_exc()}", flush=True)
            store.update_custom_request(conn, request_id, "pending")
            return

        dest = os.path.join(config.GENERATED_DIR, f"custom_{request_id}.mp4")
        size = 0
        try:
            size = os.path.getsize(dest)
        except OSError:
            pass

        # Register the clip in her give library and deliver into her character chat.
        conn.execute(
            "INSERT INTO girl_media (girl_id, kind, url, preset) "
            "VALUES (?, 'give', ?, 'custom')",
            (req["girl_id"], url),
        )
        store.log_turn(
            conn, req["user_id"], req["girl_id"],
            "I made that special video for you — it's right here. ❤️",
            req["girl_id"],
            [{"kind": "video", "url": url,
              "name": f"{req['girl_id']}-custom-video.mp4", "size": size}],
        )
        store.update_custom_request(conn, request_id, "fulfilled",
                                    video_url=url)
        print(f"[custom_video] request {request_id} fulfilled: {url} "
              f"(${cost:.4f})", flush=True)
    finally:
        conn.close()


def spawn_custom_generation(request_id: int) -> None:
    t = threading.Thread(target=run_custom_generation, args=(request_id,),
                         daemon=True, name=f"custom-video-{request_id}")
    t.start()


def run_custom_generation_with_login(request_id: int) -> None:
    """Fulfill an admin-queued customer request using the owner's saved
    Sogni login session (plan billing), then deliver it into the original
    customer's original character conversation."""
    conn = db.connect()
    ref_path = None
    try:
        row = conn.execute(
            "SELECT * FROM custom_video_requests WHERE id = ?",
            (request_id,),
        ).fetchone()
        if not row or row["status"] != "pending":
            return
        req = dict(row)

        # This is the login-based session Mia uses; no API key is needed.
        from . import assist, characters
        session = assist.stored_sogni_session(conn)
        if not session:
            print(f"[custom_video] request {request_id} waiting: Sogni login missing",
                  flush=True)
            return

        # Claim atomically so a per-request click and the batch action cannot
        # render the same request twice.
        cur = conn.execute(
            "UPDATE custom_video_requests SET status = 'generating' "
            "WHERE id = ? AND status = 'pending'",
            (request_id,),
        )
        conn.commit()
        if cur.rowcount != 1:
            return

        girl = characters.get_character(req["girl_id"], user_id=req["user_id"])
        if not girl:
            store.update_custom_request(conn, request_id, "pending")
            return

        try:
            ref_path = assist._fetch_first_frame(girl.get("avatar_url") or "")
            prompt = assist._sogni_plan_prompt(
                girl.get("name") or req["girl_id"], req["description"])
            outcome, fresh_session = assist._sogni_plan_run(
                session, prompt, ref_path, config.SOGNI_VIDEO_MODEL)
            if fresh_session:
                assist.store_sogni_session(conn, fresh_session)
            if outcome.get("status") == "session_dead":
                assist.clear_sogni_session(conn)

            if outcome.get("status") != "done" or not outcome.get("path"):
                # Keep it in the admin queue for retry/manual review. Log a
                # bounded, credential-scrubbed provider detail so failures can
                # be diagnosed without leaking prompts, session tokens or passwords.
                store.update_custom_request(conn, request_id, "pending")
                detail = str(outcome.get("detail") or "")
                detail = detail[:240]
                import re
                detail = re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", detail)
                detail = re.sub(r"(?i)(?:refresh.?token|access.?token|password)\s*[:=]\s*\S+", "[REDACTED]", detail)
                detail = " ".join(detail.split())
                print(f"[custom_video] request {request_id} not fulfilled: "
                      f"{outcome.get('status') or 'failed'}; detail={detail or 'no provider detail'}",
                      flush=True)
                return

            path = outcome["path"]
            video_url = "/generated/" + os.path.basename(path)
            size = 0
            try:
                size = os.path.getsize(path)
            except OSError:
                pass

            # Register it in the requested character's library and put a
            # real video attachment into that customer's matching chat.
            conn.execute(
                "INSERT INTO girl_media (girl_id, kind, url, preset) "
                "VALUES (?, 'give', ?, 'custom')",
                (req["girl_id"], video_url),
            )
            store.log_turn(
                conn, req["user_id"], req["girl_id"],
                "I made that special video for you — it's right here. ❤️",
                req["girl_id"],
                [{"kind": "video", "url": video_url,
                  "name": f"{req['girl_id']}-custom-video.mp4", "size": size}],
            )
            store.update_custom_request(
                conn, request_id, "fulfilled", video_url=video_url)
            print(f"[custom_video] request {request_id} fulfilled via saved Sogni login",
                  flush=True)
        except Exception as e:  # noqa: BLE001 — leave failed work in admin queue
            store.update_custom_request(conn, request_id, "pending")
            # Emit enough provider telemetry to fix the actual failure, but
            # never write credentials/session material to Railway logs.
            import re
            detail = " ".join(str(e).split())[:240]
            detail = re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", detail)
            detail = re.sub(
                r"(?i)(?:refresh.?token|access.?token|token|password|api.?key)\s*[:=]\s*\S+",
                "[REDACTED]", detail)
            detail = re.sub(r"(?i)eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}",
                            "[REDACTED_JWT]", detail)
            print(f"[custom_video] request {request_id} failed: "
                  f"{e.__class__.__name__}; detail={detail or 'no detail'}",
                  flush=True)
    finally:
        if ref_path:
            try:
                os.unlink(ref_path)
            except OSError:
                pass
        conn.close()


def spawn_custom_generation_with_login(request_id: int) -> None:
    t = threading.Thread(
        target=run_custom_generation_with_login,
        args=(request_id,),
        daemon=True,
        name=f"custom-video-login-{request_id}",
    )
    t.start()


def _run_pending_custom_generations() -> None:
    conn = db.connect()
    try:
        ids = [int(r[0]) for r in conn.execute(
            "SELECT id FROM custom_video_requests WHERE status = 'pending' "
            "ORDER BY id ASC").fetchall()]
    finally:
        conn.close()
    # Sequential renders avoid competing for the same Sogni plan/session.
    for request_id in ids:
        conn = db.connect()
        try:
            from . import assist
            has_session = bool(assist.stored_sogni_session(conn))
        finally:
            conn.close()
        if has_session:
            run_custom_generation_with_login(request_id)
        else:
            run_custom_generation(request_id)


def spawn_pending_custom_generations() -> int:
    conn = db.connect()
    try:
        from . import assist
        has_session = bool(assist.stored_sogni_session(conn))
        has_key = bool(config.SOGNI_API_KEY)
        if not (has_session or has_key):
            raise RuntimeError("sogni_login_required")
        count = int(conn.execute(
            "SELECT COUNT(*) FROM custom_video_requests "
            "WHERE status = 'pending'").fetchone()[0])
    finally:
        conn.close()
    if count:
        t = threading.Thread(
            target=_run_pending_custom_generations,
            daemon=True,
            name="custom-video-batch",
        )
        t.start()
    return count
