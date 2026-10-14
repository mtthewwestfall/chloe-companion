"""Background Sogni generation for CUSTOM video requests.

A request row is created as `pending`; the endpoint spawns a daemon thread
running `run_custom_generation(request_id)` here. The worker opens its OWN
sqlite connection (connections are not shared across threads), submits the
job to Sogni over websocket (job submission is socket-only), waits for
completion, downloads the result, saves it under config.GENERATED_DIR,
registers it in `girl_media` (kind='give', preset='custom') so it joins her
give library, and flips the request row to `fulfilled`.

Failure semantics (2026-10-14, the fallback fix): a request NEVER sits in
an unexplained `pending` limbo again — production showed all 8 historical
requests stuck there after Sogni refused them ("5000: Sensitive content
detected.") while the client sheet kept saying "queued". Now:

- A DEFINITIVE refusal/failure (Sogni content refusal, plan rejection,
  any other non-transient render failure, or an exception once the render
  was attempted) closes the request honestly: status `fallback` when the
  closest preset from THAT GIRL'S OWN give library was delivered into the
  chat instead (live_give's fallback machinery, with an in-character swap
  line), or `failed` when she has no preset to send. The refusal reason is
  recorded on the row (`error`, via assist._sogni_error_detail — the way
  give_gen records errors) and the delivered preset in `fallback_url`.
- A TRANSIENT state — Sogni reconnection throttle, dead/missing session —
  is NOT a refusal: the request parks back in `pending` with a
  "waiting: ..." note in `error` (give_gen's parking semantics), ready
  for the next admin run or the backlog sweep.

The two runners return an outcome token — "fulfilled" / "fallback" /
"failed" / "parked" / "skipped" — which the backlog sweep
(`run_custom_video_backlog_sweep`) tallies. Spawn wrappers ignore it.
Nothing is refunded because custom requests cost the user nothing up
front.

Requires the SOGNI_API_KEY Railway env var (raw key) for the API-key
path. Without it (and without a stored plan session) requests park as
waiting for a session.
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

# The line she sends with a fallback preset: she wanted to make his
# video, it didn't happen, so she sends one of hers instead — in her
# voice, with no system-speak and no mention of filters or errors.
_FALLBACK_LINE = ("that one got away from me, babe — wasn't about to "
                  "leave you waiting, so here's one of mine instead. 💋")

# give_gen's waiting words for a session park (row stays `pending`,
# the reason rides in `error`).
_WAITING_SESSION = "waiting: session unavailable"


def _scrub_detail(text: Any) -> str:
    """Bound provider detail for LOG lines: whitespace-collapsed,
    length-capped, credential-shaped substrings redacted. (The error
    recorded on the row itself comes from assist._sogni_error_detail,
    the way give_gen records errors.)"""
    import re
    detail = " ".join(str(text or "").split())[:240]
    detail = re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", detail)
    detail = re.sub(
        r"(?i)(?:refresh.?token|access.?token|token|password|api.?key)"
        r"\s*[:=]\s*\S+", "[REDACTED]", detail)
    detail = re.sub(
        r"(?i)eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}",
        "[REDACTED_JWT]", detail)
    return detail


def _deliver_preset_fallback(conn, req: dict[str, Any]) -> str:
    """Deliver the closest preset from THIS GIRL'S OWN give library
    into the requesting user's chat with her — live_give's selection
    and delivery machinery (stage walk over her own girl_media, then
    her static library), never another girl's media and never Chloe's
    fallback pool. Returns the delivered media URL, or "" when she
    has no preset to send."""
    from . import live_give
    preset = live_give._pick_preset(
        conn, req["user_id"], req["girl_id"], "video")
    if not preset:
        return ""
    live_give._deliver_preset(
        conn, req["user_id"], req["girl_id"], preset,
        during_render=False, line=_FALLBACK_LINE)
    return preset["url"]


def _close_request_with_fallback(conn, req: dict[str, Any],
                                 error_text: str) -> str:
    """Definitive refusal/failure: close the request honestly and send
    her preset instead. Returns "fallback" (preset delivered; row
    records the reason and the preset URL) or "failed" (no preset
    available; row records the reason)."""
    request_id = req["id"]
    fallback_url = ""
    try:
        fallback_url = _deliver_preset_fallback(conn, req)
    except Exception as e:  # noqa: BLE001 — a failed fallback must
        # still leave an honest terminal row behind.
        print(f"[custom_video] request {request_id} fallback delivery "
              f"failed: {e}", flush=True)
    if fallback_url:
        store.update_custom_request(
            conn, request_id, "fallback",
            error=error_text, fallback_url=fallback_url)
        print(f"[custom_video] request {request_id} closed as fallback: "
              f"{error_text}; preset fallback delivered {fallback_url}",
              flush=True)
        return "fallback"
    store.update_custom_request(conn, request_id, "failed",
                                error=error_text)
    print(f"[custom_video] request {request_id} closed as failed: "
          f"{error_text}; no preset fallback available", flush=True)
    return "failed"


def _park_custom_request(conn, request_id: int, error_text: str,
                         reason: str) -> str:
    """Transient state (throttle / session unavailable): NOT a
    refusal. The row returns to `pending` with the waiting note in
    `error` — give_gen's parking semantics — so a later admin run or
    the backlog sweep picks it up again. Returns "parked"."""
    store.update_custom_request(conn, request_id, "pending",
                                error=error_text)
    print(f"[custom_video] request {request_id} parked: {reason}",
          flush=True)
    return "parked"


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


def run_custom_generation(request_id: int) -> str:
    """Thread entry (API-key path): try Sogni; on success register the
    clip. A failed render attempt is definitive for this path (there
    is no session/throttle state to wait out here): close the request
    honestly with a preset fallback. Returns the outcome token."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM custom_video_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if not row or row["status"] != "pending":
            return "skipped"
        req = dict(row)

        cur = conn.execute(
            "UPDATE custom_video_requests SET status = 'generating' "
            "WHERE id = ? AND status = 'pending'",
            (request_id,),
        )
        conn.commit()
        if cur.rowcount != 1:
            return "skipped"

        # Resolve the girl for face-lock / naming. Import here to avoid a
        # hard import cycle (characters imports config only, but be safe).
        from . import assist, characters
        girl = characters.get_character(req["girl_id"], user_id=req["user_id"])
        if not girl:
            store.update_custom_request(conn, request_id, "pending")
            return "skipped"

        try:
            url, cost = asyncio.run(
                _generate_custom_async(request_id, girl, req["description"]))
        except Exception as e:  # noqa: BLE001
            # Sogni block ("Sensitive content detected") or any other
            # render failure: the request closes honestly with the
            # recorded reason and her preset fallback — never back to
            # an unexplained `pending` (2026-10-14 fallback fix).
            print(f"[custom_video] request {request_id} not auto-fulfilled: {e}\n"
                  f"{traceback.format_exc()}", flush=True)
            return _close_request_with_fallback(
                conn, req, assist._sogni_error_detail("failed", str(e), e))

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
        return "fulfilled"
    finally:
        conn.close()


def spawn_custom_generation(request_id: int) -> None:
    t = threading.Thread(target=run_custom_generation, args=(request_id,),
                         daemon=True, name=f"custom-video-{request_id}")
    t.start()


def run_custom_generation_with_login(request_id: int) -> str:
    """Fulfill an admin-queued customer request using the owner's saved
    Sogni login session (plan billing), then deliver it into the original
    customer's original character conversation.

    Outcome token: "fulfilled" on a real render; "parked" when the
    session path is transiently unavailable (no stored session,
    throttled, still session-dead after recovery) — the row stays
    `pending` with the waiting reason recorded; "fallback" / "failed"
    when the render definitively refused or failed and the request was
    closed honestly (preset delivered, or none available); "skipped"
    when the row was not pending-claimable."""
    conn = db.connect()
    ref_path = None
    try:
        row = conn.execute(
            "SELECT * FROM custom_video_requests WHERE id = ?",
            (request_id,),
        ).fetchone()
        if not row or row["status"] != "pending":
            return "skipped"
        req = dict(row)

        # This is the login-based session Mia uses; no API key is needed.
        from . import assist, characters
        session = assist.stored_sogni_session(conn)
        if not session:
            # No session is a wait, not a refusal: park with the
            # waiting note recorded (the pre-fix code only logged it,
            # which is how rows sat unexplained for days).
            print(f"[custom_video] request {request_id} waiting: Sogni login missing",
                  flush=True)
            return _park_custom_request(
                conn, request_id, _WAITING_SESSION, "Sogni login missing")

        # Claim atomically so a per-request click and the batch action cannot
        # render the same request twice.
        cur = conn.execute(
            "UPDATE custom_video_requests SET status = 'generating' "
            "WHERE id = ? AND status = 'pending'",
            (request_id,),
        )
        conn.commit()
        if cur.rowcount != 1:
            return "skipped"

        girl = characters.get_character(req["girl_id"], user_id=req["user_id"])
        if not girl:
            store.update_custom_request(conn, request_id, "pending")
            return "skipped"

        try:
            ref_path = assist._fetch_first_frame(girl.get("avatar_url") or "")
            prompt = assist._sogni_plan_prompt(
                girl.get("name") or req["girl_id"], req["description"])
            outcome, fresh_session = assist._sogni_plan_run(
                session, prompt, ref_path, config.SOGNI_VIDEO_MODEL)
            if fresh_session:
                assist.store_sogni_session(conn, fresh_session)
            if outcome.get("status") == "session_dead":
                # Non-destructive recovery: the pair is cleared (env
                # seed tombstoned) only when it is confirmed dead and
                # a re-login has also failed — see
                # assist.resolve_dead_sogni_session.
                assist.resolve_dead_sogni_session(
                    conn, session, caller="custom_video")

            if outcome.get("status") != "done" or not outcome.get("path"):
                status_word = outcome.get("status") or "failed"
                if status_word == "throttled":
                    # Sogni's reconnection throttle (socket 4056): not
                    # a refusal and not a dead session — park with the
                    # wait recorded, give_gen-style; a later run or
                    # the sweep retries once it lifts.
                    seconds = outcome.get("throttle_seconds") \
                        or assist._SOGNI_THROTTLE_DEFAULT_SECONDS
                    minutes = max(1, round(seconds / 60))
                    note = f"Sogni throttled (retry after ~{minutes} min)"
                    return _park_custom_request(
                        conn, request_id, f"waiting: {note}", note)
                if status_word == "session_dead":
                    # Still dead after the seam heal and the resolve
                    # above: the session is unavailable, not the
                    # request's fault. Park, don't close.
                    return _park_custom_request(
                        conn, request_id, _WAITING_SESSION,
                        "session unavailable")
                # Definitive: content refusal ("5000: Sensitive content
                # detected."), plan rejection, or any other real render
                # failure. Record the reason the way give_gen records
                # errors and close honestly with her preset fallback —
                # the pre-fix code silently reset these to `pending`,
                # which is why the sheet said "queued" forever.
                error_text = assist._sogni_error_detail(
                    status_word, outcome.get("detail"))
                print(f"[custom_video] request {request_id} not fulfilled: "
                      f"{status_word}; detail={_scrub_detail(error_text)}",
                      flush=True)
                return _close_request_with_fallback(conn, req, error_text)

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
            return "fulfilled"
        except Exception as e:  # noqa: BLE001 — a crashed render closes
            # honestly like any other definitive failure (telemetry
            # logged scrubbed, as before).
            print(f"[custom_video] request {request_id} failed: "
                  f"{e.__class__.__name__}; "
                  f"detail={_scrub_detail(e) or 'no detail'}",
                  flush=True)
            return _close_request_with_fallback(
                conn, req, assist._sogni_error_detail("failed", str(e), e))
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


# ---------------------------------------------------------------------------
# Backlog sweep (2026-10-14): one-shot processing of every pending
# custom request under the fallback-fix semantics.
# ---------------------------------------------------------------------------

def run_custom_video_backlog_sweep(
        known_refused_ids: Any = (),
        known_refused_error: str = "") -> dict[str, Any]:
    """Process EVERY currently-pending custom video request, once, in
    id order, and report what happened to each.

    - Ids in `known_refused_ids` were already definitively refused
      (production-log evidence — e.g. request #8's "5000: Sensitive
      content detected." on 2026-10-14): they are NOT rendered again.
      They close honestly with `known_refused_error` recorded as the
      reason and the preset fallback delivered.
    - Every other pending request gets ONE render attempt through the
      usual path choice (stored plan session -> login runner, else the
      API-key runner): success fulfills it, a definitive refusal closes
      it with the fallback, and a throttle/session park leaves it
      `pending` with the waiting note — reported here as "parked",
      never as failed.

    Returns {"processed", "fulfilled", "fallback", "failed", "parked",
    "skipped", "results": {request_id: token}} and prints the same
    summary as one [custom_video] log line."""
    known = {int(i) for i in (known_refused_ids or ())}
    refused_error = (known_refused_error or "").strip() \
        or "render refused before this sweep (not re-rendered)"
    conn = db.connect()
    try:
        ids = [int(r[0]) for r in conn.execute(
            "SELECT id FROM custom_video_requests WHERE status = 'pending' "
            "ORDER BY id ASC").fetchall()]
        from . import assist
        has_session = bool(assist.stored_sogni_session(conn))
    finally:
        conn.close()
    report: dict[str, Any] = {
        "processed": 0, "fulfilled": 0, "fallback": 0, "failed": 0,
        "parked": 0, "skipped": 0, "results": {},
    }
    for request_id in ids:
        if request_id in known:
            conn = db.connect()
            try:
                row = conn.execute(
                    "SELECT * FROM custom_video_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()
                if not row or row["status"] != "pending":
                    token = "skipped"
                else:
                    token = _close_request_with_fallback(
                        conn, dict(row), refused_error)
            finally:
                conn.close()
        elif has_session:
            token = run_custom_generation_with_login(request_id)
        else:
            token = run_custom_generation(request_id)
        report["processed"] += 1
        report[token] = report.get(token, 0) + 1
        report["results"][request_id] = token
    print(f"[custom_video] backlog sweep complete: {report}", flush=True)
    return report


def spawn_custom_video_backlog_sweep(
        known_refused_ids: Any = (),
        known_refused_error: str = "") -> int:
    """Spawn `run_custom_video_backlog_sweep` on a daemon thread (the
    admin endpoint's shape, mirroring spawn_pending_custom_generations)
    and return how many requests are pending. Unlike the batch spawn,
    a missing Sogni session does NOT block the sweep: known-refused
    rows close without any render, and render attempts simply park."""
    conn = db.connect()
    try:
        count = int(conn.execute(
            "SELECT COUNT(*) FROM custom_video_requests "
            "WHERE status = 'pending'").fetchone()[0])
    finally:
        conn.close()
    if count:
        t = threading.Thread(
            target=run_custom_video_backlog_sweep,
            args=(list(known_refused_ids or ()), known_refused_error),
            daemon=True,
            name="custom-video-backlog-sweep",
        )
        t.start()
    return count
