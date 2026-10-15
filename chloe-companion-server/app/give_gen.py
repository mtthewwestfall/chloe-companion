"""Give generations for public characters — daily 15s Sogni generations per give stage.

For each public character and each give stage (1..9), a generation is queued daily.
Renders use the saved Sogni login session with duration set to 15 seconds.
Finished generations land as `pending` for admin review on the admin page.
The admin can `approve` (adds the video to girl_media) or `delete` (marks as deleted).
"""
from __future__ import annotations

import asyncio
import collections
import os
import subprocess
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any

from . import assist, characters, config, db

STAGE_NAMES = {
    1: "lift tease",
    2: "shirt off",
    3: "boob grabs",
    4: "nipple pinch",
    5: "pants off",
    6: "plays w/ herself",
    7: "titty fuck",
    8: "oral",
    9: "sex",
}

STAGE_DESCRIPTIONS = {
    1: "lift tease, lifting her shirt playfully, teasing smile, she whispers \"Like what you see?\"",
    2: "shirt off, topless, holding her top, bedroom setting, she says \"Much better, don't you think?\"",
    3: "boob grabs, touching and holding her breasts, sensual expression, soft moans",
    4: "nipple pinch, gently pinching her nipples, passionate smile, she gasps \"Oh, that feels so good\"",
    5: "pants off, wearing only underwear, sexy pose, she teases \"Want to see the rest?\"",
    6: "plays with herself, touching herself intimately, passionate movement, breathy moans, she whispers \"I wish it was you touching me\"",
    7: "titty fuck, intimate chest scene, close up, she moans",
    8: "oral, intimate oral scene, close up",
    9: "sex, passionate intimate scene, she cries out \"Yes, right there\"",
}

# Male-character versions of the give stages. The daily auto-generator picks
# these for characters whose derived gender is male (Jake, Eli) instead of
# the female maps above.
MALE_STAGE_NAMES = {
    1: "shirt lift tease",
    2: "shirt off",
    3: "chest flex",
    4: "nipple tease",
    5: "pants off",
    6: "strokes himself",
    7: "deep thrusts",
    8: "getting head",
    9: "fucking",
}

MALE_STAGE_DESCRIPTIONS = {
    1: "shirt lift tease, lifting his shirt playfully, teasing grin, showing off his torso, he says \"Enjoy the view\"",
    2: "shirt off, shirtless, holding his shirt, bedroom setting, he says \"Better?\"",
    3: "chest flex, flexing his chest and arms, strong masculine expression",
    4: "nipple tease, thumbing his nipples slowly, teasing grin",
    5: "pants off, down to his underwear, confident masculine pose, he says \"Your turn\"",
    6: "stroking himself, working his cock slowly, hungry masculine expression, he groans \"God, you turn me on\"",
    7: "deep thrusts, pounding rhythm, intense close up",
    8: "getting head, intimate oral scene, his pleasure close up, he groans",
    9: "fucking, passionate hard thrusting, intimate scene, he says \"You feel so good\"",
}


def _is_male_character(girl_id: str) -> bool:
    """True when the character's derived gender is male (Jake, Eli)."""
    try:
        return characters.character_gender(girl_id) == "male"
    except Exception:
        return False


def stage_name_for(girl_id: str, stage: int) -> str:
    """Display name for a give stage, gender-aware."""
    names = MALE_STAGE_NAMES if _is_male_character(girl_id) else STAGE_NAMES
    return names.get(stage, f"stage {stage}")


def stage_description_for(girl_id: str, stage: int) -> str:
    """Generation prompt for a give stage, gender-aware."""
    descs = MALE_STAGE_DESCRIPTIONS if _is_male_character(girl_id) else STAGE_DESCRIPTIONS
    return descs.get(stage, f"stage {stage} give video")


def list_give_generations(conn: Any, status: str | None = None,
                          girl_id: str | None = None,
                          day: str | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM give_generations"
    clauses = []
    params: list[Any] = []

    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)
    if girl_id:
        clauses.append("girl_id = ?")
        params.append(girl_id.lower().strip())
    if day:
        clauses.append("day = ?")
        params.append(day)

    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC"

    rows = conn.execute(query, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["stage_name"] = stage_name_for(str(d.get("girl_id") or ""), d.get("stage", 1))
        out.append(d)
    return out


def get_give_generation(conn: Any, gen_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM give_generations WHERE id = ?", (gen_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["stage_name"] = stage_name_for(str(d.get("girl_id") or ""), d.get("stage", 1))
    return d


def create_give_generation(conn: Any, girl_id: str, stage: int,
                            prompt: str, day: str,
                            status: str = "pending") -> int:
    cur = conn.execute(
        "INSERT INTO give_generations (girl_id, stage, prompt, day, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (girl_id.lower().strip(), stage, prompt, day, status),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_give_generation_status(conn: Any, gen_id: int, status: str,
                                   url: str = "", error: str = "") -> None:
    conn.execute(
        "UPDATE give_generations SET status = ?, url = ?, error = ? WHERE id = ?",
        (status, url, error, gen_id),
    )
    conn.commit()


def approve_give_generation(conn: Any, gen_id: int) -> dict[str, Any] | None:
    gen = get_give_generation(conn, gen_id)
    if not gen:
        return None
    if gen["status"] == "approved":
        return gen

    # Insert into girl_media so it joins her give library
    cols = {r[1] for r in conn.execute("PRAGMA table_info(girl_media)")}
    if "stage" in cols:
        conn.execute(
            "INSERT INTO girl_media (girl_id, kind, url, preset, stage) "
            "VALUES (?, 'give', ?, 'give', ?)",
            (gen["girl_id"], gen["url"], gen["stage"]),
        )
    else:
        conn.execute(
            "INSERT INTO girl_media (girl_id, kind, url, preset) "
            "VALUES (?, 'give', ?, 'give')",
            (gen["girl_id"], gen["url"]),
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE give_generations SET status = 'approved', approved_at = ? WHERE id = ?",
        (now_iso, gen_id),
    )
    conn.commit()
    return get_give_generation(conn, gen_id)


def delete_give_generation(conn: Any, gen_id: int) -> dict[str, Any] | None:
    gen = get_give_generation(conn, gen_id)
    if not gen:
        return None
    conn.execute(
        "UPDATE give_generations SET status = 'deleted' WHERE id = ?",
        (gen_id,),
    )
    conn.commit()
    return get_give_generation(conn, gen_id)


_APPROVED_REF_DIR = os.path.join(os.path.dirname(__file__), "ref_frames")


def _approved_reference_frame(girl_id: str) -> str | None:
    """Owner-approved reference photo on disk, if one exists.

    Slot-name caveat (2026-10-14): for brooklyn, hannah, jordan and
    mia the file in this slot intentionally holds a CLOTHED starting
    frame (extracted from the girl's own stage-1 give clip), not a
    topless photo. Controlled tests that day proved Sogni's filter
    refuses the render prompt's undressing language, not the
    starting image, and the passing renders started clothed — so the
    stage>2 starting frame for those four is clothed by design now.
    The other girls' files here are the topless photos the slot was
    named for."""
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        path = os.path.join(_APPROVED_REF_DIR, f"{girl_id}-topless{ext}")
        if os.path.exists(path):
            return path
    return None


def _topless_reference_frame(conn: Any, girl_id: str) -> str | None:
    """Reference frame for stages past 2, where the top is already off.

    Prefers the owner-approved topless photo on disk, then the last
    frame of the approved stage-2 ("shirt off") give video (the first
    frame still has the top on). Returns None when unavailable so the
    caller falls back to the avatar.

    The on-disk photo is handed over as a temp COPY, never as
    itself: every render path unlinks the ref_path it was given when
    the render finishes (the other branches return disposable
    temp files), so returning the curated file would delete it on
    first use — the same "callers delete what they are handed, the
    library original must survive" rule media_gen already follows.
    """
    approved = _approved_reference_frame(girl_id)
    if approved:
        try:
            with open(approved, "rb") as fh:
                data = fh.read()
            fd, tmp = tempfile.mkstemp(
                prefix="give-ref-",
                suffix=os.path.splitext(approved)[1])
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            return tmp
        except OSError:
            pass
    try:
        # Latest stage-2 video preferred (last frame = top off); a photo
        # is only used when no video exists.
        row = conn.execute(
            "SELECT url FROM girl_media WHERE girl_id = ? AND kind = 'give' "
            "AND stage = 2 ORDER BY (lower(url) LIKE '%.jpg' "
            "OR lower(url) LIKE '%.jpeg' OR lower(url) LIKE '%.png' "
            "OR lower(url) LIKE '%.webp'), id DESC LIMIT 1",
            (girl_id,)).fetchone()
    except Exception:
        return None
    video_url = (row[0] or "").strip() if row else ""
    if not video_url:
        return None
    video_path = assist._fetch_first_frame(video_url)
    if not video_path:
        return None
    # A stage-2 photo needs no frame extraction — use it as-is.
    if video_url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp")):
        return video_path
    try:
        exe = assist._ffmpeg_exe()
        if not exe:
            return None
        fd, frame_path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        try:
            proc = subprocess.run(
                [exe, "-hide_banner", "-loglevel", "error",
                 "-sseof", "-0.5", "-i", video_path,
                 "-frames:v", "1", "-q:v", "5", "-y", frame_path],
                capture_output=True, timeout=45)
        except (OSError, subprocess.SubprocessError):
            return None
        if (proc.returncode == 0 and os.path.exists(frame_path)
                and os.path.getsize(frame_path) > 0):
            return frame_path
        try:
            os.unlink(frame_path)
        except OSError:
            pass
        return None
    finally:
        try:
            os.unlink(video_path)
        except OSError:
            pass


def _hardwired_login(girl_id: str) -> tuple[str, str] | None:
    """The server-side Sogni login for a private character, or None
    when she is not owner-scoped or the login is not configured."""
    try:
        if not characters.owner_of(girl_id):
            return None
    except Exception:  # noqa: BLE001
        return None
    username = (os.environ.get("SOGNI_LOGIN_USERNAME") or "").strip()
    password = os.environ.get("SOGNI_LOGIN_PASSWORD") or ""
    if not username or not password:
        return None
    return (username, password)


def _hardwired_sogni_session(conn, girl_id: str,
                             caller: str = "give_gen") -> dict | None:
    """Private characters only: when no stored Sogni session is usable,
    sign in fresh with the server-side login kept in the
    SOGNI_LOGIN_USERNAME / SOGNI_LOGIN_PASSWORD env vars and store the
    resulting pair. Public characters never trigger this sign-in.

    The sign-in goes through assist's single-flight gate: the login is
    shared by every feature, and a sign-in racing another feature's
    sign-in invalidates the pair that feature just stored. ``caller``
    names the feature that asked (give_gen itself, or live_give,
    which borrows this helper) for the sign-in failure log tag."""
    login = _hardwired_login(girl_id)
    if not login:
        return None
    session = assist.sogni_login_single_flight(conn, login,
                                               caller=caller)
    if not session:
        print(f"[give_gen] hardwired Sogni sign-in failed for {girl_id}",
              flush=True)
        return None
    print(f"[give_gen] hardwired Sogni sign-in ok for {girl_id}", flush=True)
    return session


def _park_give_generation(conn, gen_id: int,
                          error: str = "waiting: session unavailable",
                          reason: str = "session unavailable") -> str:
    """Leave a job pending for a later drain instead of failing it:
    the Sogni session is temporarily unavailable (dead session
    unhealed, login missing, login cooldown) — a later trigger, the
    boot loop, or the processor's next sweep re-processes the row
    once the session recovers. The row keeps status "pending" with
    no url, so the trigger's dedup still counts it as existing for
    its (girl, stage, day) and no duplicate is ever queued. Returns
    the "parked" signal the single processor stands down on."""
    update_give_generation_status(
        conn, gen_id, "pending", error=error)
    print(f"[give_gen] gen #{gen_id} parked: {reason}",
          flush=True)
    return "parked"


def _run_give_generation_job(gen_id: int) -> str | None:
    """Worker thread: render 15s Sogni video using the saved Sogni login session.

    Returns "parked" when the session path ended unavailable
    (session_dead unhealed, login missing, login cooldown, or a
    Sogni reconnection throttle): the row
    was left pending, not failed, and the caller — the single
    processor — stands down for this drain. Returns None otherwise
    (rendered, or genuinely failed: blocked / plan_rejected / other
    real render errors still fail exactly as before)."""
    conn = db.connect()
    ref_path = None
    try:
        gen = get_give_generation(conn, gen_id)
        if not gen or gen["status"] not in ("pending", "generating"):
            return None

        session = assist.stored_sogni_session(conn)
        if not session:
            session = _hardwired_sogni_session(conn, gen["girl_id"])
        if not session:
            return _park_give_generation(conn, gen_id)

        update_give_generation_status(conn, gen_id, "generating")

        # Private characters only resolve for their owner; without the
        # owner they silently fall back to Chloe's face and name.
        girl = characters.get_character(
            gen["girl_id"], user_id=characters.owner_of(gen["girl_id"]))
        stage = gen.get("stage") or 0
        ref_path = None
        if stage > 2:
            ref_path = _topless_reference_frame(conn, gen["girl_id"])
        if not ref_path:
            ref_path = assist._fetch_first_frame(girl.get("avatar_url") or "")
        if not ref_path:
            err_msg = "no reference photo for " + str(gen["girl_id"])
            update_give_generation_status(conn, gen_id, "failed", error=err_msg)
            print(f"[give_gen] gen #{gen_id} failed: {err_msg}", flush=True)
            return
        prompt = assist._sogni_plan_prompt(
            girl.get("name") or gen["girl_id"], gen["prompt"]
        )

        outcome, fresh_session = assist._sogni_plan_run(
            session, prompt, ref_path, model=config.SOGNI_VIDEO_MODEL, duration=15
        )
        if fresh_session:
            assist.store_sogni_session(conn, fresh_session)
        if outcome.get("status") == "session_dead" \
                and not outcome.get("seam_healed"):
            # Non-destructive recovery (assist.resolve_dead_sogni_session):
            # when another feature already refreshed the shared login,
            # retry once with the newest stored pair; only when the
            # stored pair is the one that just died does a single-flight
            # hardwired sign-in happen, and the pair is cleared (env
            # seed tombstoned) only after a DEFINITIVE sign-in failure
            # (a transient 429/5xx/transport failure or an active login
            # cooldown leaves the pair untouched). Skipped
            # when the seam already healed and re-ran this render
            # (outcome["seam_healed"]): a second recovery here would
            # stack another sign-in on the same render.
            retry_session = assist.resolve_dead_sogni_session(
                conn, session, login=_hardwired_login(gen["girl_id"]),
                caller="give_gen")
            if retry_session:
                outcome, fresh_session = assist._sogni_plan_run(
                    retry_session, prompt, ref_path,
                    model=config.SOGNI_VIDEO_MODEL, duration=15
                )
                if fresh_session:
                    assist.store_sogni_session(conn, fresh_session)

        if outcome.get("status") == "session_dead":
            # Still dead after the seam heal AND this job's recovery
            # (or the login cooldown refused the recovery sign-in):
            # the session is unavailable, not the job's fault. Park
            # it pending for the next drain instead of failing it —
            # failing here is what burned a whole queue in seconds
            # whenever the session died (2026-10-14).
            return _park_give_generation(conn, gen_id)

        if outcome.get("status") == "throttled":
            # Sogni throttled the account's reconnections (socket
            # 4056): not the job's fault and NOT a dead session —
            # nobody signs in or reconnects over it (assist's seam
            # already set the shared login cooldown for the stated
            # wait). Park exactly like a session park — the single
            # processor stands down on "parked" — with the wait in
            # the row's error so the queue says why it is waiting.
            seconds = outcome.get("throttle_seconds") \
                or assist._SOGNI_THROTTLE_DEFAULT_SECONDS
            minutes = max(1, round(seconds / 60))
            note = f"Sogni throttled (retry after ~{minutes} min)"
            return _park_give_generation(
                conn, gen_id, error=f"waiting: {note}", reason=note)

        if outcome.get("status") != "done" or not outcome.get("path"):
            # Never a blank error: production rows carried a
            # two-space error when the render's detail came back
            # whitespace (see assist._sogni_error_detail).
            err_msg = assist._sogni_error_detail(
                outcome.get("status") or "failed",
                outcome.get("detail"))
            update_give_generation_status(conn, gen_id, "failed", error=err_msg)
            print(f"[give_gen] gen #{gen_id} failed: {err_msg}", flush=True)
            return

        path = outcome["path"]
        video_url = "/generated/" + os.path.basename(path)
        update_give_generation_status(conn, gen_id, "pending", url=video_url)
        print(f"[give_gen] gen #{gen_id} ready: {video_url} (15s)", flush=True)

    except Exception as e:  # noqa: BLE001
        err_msg = assist._sogni_error_detail("failed", str(e), e)
        update_give_generation_status(conn, gen_id, "failed", error=err_msg)
        print(f"[give_gen] gen #{gen_id} exception: {e}\n{traceback.format_exc()}", flush=True)
    finally:
        if ref_path:
            try:
                os.unlink(ref_path)
            except OSError:
                pass
        conn.close()


def spawn_give_generation_job(gen_id: int) -> None:
    # Route through the single processor (below): a dedicated render
    # thread per gen was one of the two concurrent Sogni clients that
    # destroyed each other's session in production. The spawned
    # thread only wakes the processor, which either becomes the
    # processor or queues behind the running one.
    #
    # This is the chat-driven single-id hand-off (assist's studio
    # renders): a caller is actively polling for this row, so the id
    # goes to the processor's PRIORITY lane — the one lane that may
    # pass the pending backlog. Batch triggers do NOT get this lane
    # (see _process_queued_give_generations).
    with _PROCESSOR_GUARD:
        _PRIORITY_GIVE_IDS.append(gen_id)
    t = threading.Thread(
        target=_process_queued_give_generations,
        args=([],),
        daemon=True,
        name=f"give-gen-{gen_id}",
    )
    t.start()


def trigger_daily_give_generations(day: str | None = None,
                                   include_private: bool = False) -> list[int]:
    """Check and queue daily give generations for characters x 9 stages.

    For each character (public only by default; private owner-scoped too when
    include_private=True) and for each give stage (1..9), if no generation
    exists for the given day (day=YYYY-MM-DD, defaults to today), queue a new
    generation job. Returns list of queued give generation IDs.
    """
    conn = db.connect()
    queued_ids: list[int] = []
    try:
        today_str = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Public characters only by default; private too when requested.
        # (get_characters filters private out unless asked, so request them
        # explicitly when include_private is set.)
        chars = [c for c in characters.get_characters(include_private=include_private)
                 if include_private or not c.get("owner_user_id")]

        for char in chars:
            gid = char["id"].lower().strip()
            girl_name = char.get("name") or gid

            for stage in range(1, 10):
                # Check if generation exists for today
                existing = conn.execute(
                    "SELECT id FROM give_generations WHERE girl_id = ? AND stage = ? AND day = ? "
                    "AND status IN ('pending', 'generating', 'approved')",
                    (gid, stage, today_str),
                ).fetchone()

                if existing:
                    continue

                desc = stage_description_for(gid, stage)
                gen_id = create_give_generation(conn, gid, stage, desc, today_str, status="pending")
                queued_ids.append(gen_id)

    finally:
        conn.close()

    # Spawn background generation jobs
    if queued_ids:
        _spawn_processor(queued_ids, "daily-give-batch")
    elif _has_unrendered_pending():
        # Nothing new to queue, but a parked backlog exists (rows a
        # dead session left pending): a bare trigger must still kick
        # the processor's sweep, or the queue only ever moves on a
        # boot. (2026-10-14: an admin re-probe queued 0 rows and the
        # parked queue never drained.)
        _spawn_processor([], "daily-give-sweep")

    return queued_ids


def _spawn_processor(gen_ids: list[int], name: str) -> None:
    t = threading.Thread(
        target=_process_queued_give_generations,
        args=(list(gen_ids),),
        daemon=True,
        name=name,
    )
    t.start()


def _has_unrendered_pending() -> bool:
    """Any queued row still waiting for its render (status pending,
    no result url) — the sweep fodder a bare trigger must kick."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id FROM give_generations WHERE status = 'pending' "
            "AND (url IS NULL OR url = '') LIMIT 1").fetchone()
        return row is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Single give processor: exactly one worker thread drains the queue.
#
# Production failure (2026-10-13): the boot auto-loop's batch and a
# manual generate-daily batch ran as two processor threads at once.
# Each render creates its own Sogni client, the second client's
# session superseded the first's, and both batches died at the SDK's
# account check — the single-flight sign-in lock could not help,
# because no sign-in was involved. So every trigger now wakes ONE
# shared processor: the first caller to find the processor idle
# becomes it; callers that find it running just leave their wake-up
# behind and return. There is never a second processor.
#
# Selection order (2026-10-14 drain fix): the processor does NOT
# work from the ids a trigger hands in. Handed-in batch ids used to
# be popped first, so every fresh trigger batch (boot auto-loop,
# 12h tick, admin generate-daily) cut in line ahead of the pending
# backlog — and when a job parked (throttle / dead session) the
# processor stood down with the rest of that batch still sitting
# in the in-memory hand-in deque, where the next deploy silently
# discarded it; those rows stayed pending for a day while fresh
# batches rendered around them. Now the work selection is:
#   1. the priority lane (single-id hand-offs from
#      spawn_give_generation_job — a chat caller is polling for
#      that one row);
#   2. the DB claim: the OLDEST unrendered pending row, in global
#      id order — trigger rows are DB rows and take their turn
#      behind everything older, so the backlog drains in order
#      no matter how often triggers fire, and a park stand-down
#      strands nothing (the next wake re-derives the work from
#      the DB, never from memory);
#   3. the hand-in deque, only as a fallback for ids whose rows
#      are not claimable from the DB.
# The processor stands down only when all three are empty, or the
# moment a job parks (the park discipline is unchanged: a parked
# job ends the drain, the queue stays pending for the next wake).
_PROCESSOR_GUARD = threading.Lock()
_PENDING_GIVE_IDS: collections.deque[int] = collections.deque()
_PRIORITY_GIVE_IDS: collections.deque[int] = collections.deque()
_GIVE_PROCESSOR_ACTIVE = False

# Deploy-overlap boot grace: right after a deploy, the old and new
# containers briefly coexist and both fire their boot drains; two
# live processors on one Sogni login invalidate each other's
# session (and now trip the login rate limit healing it). The FIRST
# drain of a process therefore waits out the remainder of a grace
# window measured from process start before its first job — by the
# time it renders, the old container is gone. Armed by the app boot
# path (arm_boot_grace, next to the auto daily-gives loop in
# main.py); unarmed in tests and import-only contexts, where there
# is no second container to fight. GIVE_GEN_BOOT_GRACE_SECONDS
# overrides the window (0 disables it outright).
_PROCESS_STARTED_AT = time.time()
_BOOT_GRACE_SECONDS = 90.0
_BOOT_GRACE_ARMED = False


def arm_boot_grace() -> None:
    """Arm the deploy-overlap boot grace for this process."""
    global _BOOT_GRACE_ARMED
    _BOOT_GRACE_ARMED = True


def _boot_grace_wait() -> None:
    """Sleep out the rest of the boot grace window (processor
    threads only — a plain sleep is fine, the processor is a
    background thread). No-op when unarmed or the window passed."""
    if not _BOOT_GRACE_ARMED:
        return
    try:
        grace = float(os.environ.get("GIVE_GEN_BOOT_GRACE_SECONDS")
                      or _BOOT_GRACE_SECONDS)
    except (TypeError, ValueError):
        grace = _BOOT_GRACE_SECONDS
    remaining = grace - (time.time() - _PROCESS_STARTED_AT)
    if remaining > 0:
        print(f"[give_gen] boot grace: first drain waits "
              f"{remaining:.0f}s (deploy-overlap window)", flush=True)
        time.sleep(remaining)


# Live-render priority pause (2026-10-14): between jobs the
# processor waits out any live chat render holding assist's
# live-render signal — a live request is a person waiting in chat,
# this queue is background. The pause is bounded so a wedged signal
# can never stall the queue forever (a live render's own ceiling is
# its 900s project wait).
_LIVE_PAUSE_POLL_S = 0.5
_LIVE_PAUSE_MAX_S = 1200.0


def _wait_out_live_render() -> None:
    waited = 0.0
    while assist.live_render_active():
        if waited <= 0.0:
            print("[give_gen] pausing between jobs: live render "
                  "in flight", flush=True)
        if waited >= _LIVE_PAUSE_MAX_S:
            print("[give_gen] live-render pause hit its cap; "
                  "resuming the queue", flush=True)
            return
        time.sleep(_LIVE_PAUSE_POLL_S)
        waited += _LIVE_PAUSE_POLL_S


def _claim_next_queued_give_id(exclude: set[int]) -> int | None:
    """Oldest unrendered queued generation (status 'pending' with no
    result url yet), skipping ids this processor run already handled,
    or None when nothing is left to drain."""
    conn = db.connect()
    try:
        if exclude:
            marks = ",".join("?" for _ in exclude)
            row = conn.execute(
                "SELECT id FROM give_generations WHERE status = 'pending' "
                "AND (url IS NULL OR url = '') AND id NOT IN (" + marks + ") "
                "ORDER BY id ASC LIMIT 1", tuple(exclude)).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM give_generations WHERE status = 'pending' "
                "AND (url IS NULL OR url = '') "
                "ORDER BY id ASC LIMIT 1").fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def _discard_handed_in(gen_id: int) -> None:
    """Drop a selected id from the hand-in deque when it is there:
    the deque is only a fallback lane now, and a row the priority
    lane or the DB claim already selected must not be handed to the
    fallback later in the same drain."""
    with _PROCESSOR_GUARD:
        try:
            _PENDING_GIVE_IDS.remove(gen_id)
        except ValueError:
            pass


def _process_queued_give_generations(gen_ids: list[int]) -> None:
    """Wake the single give processor (handed-in ids are a wake-up,
    not a work order — see the selection-order note above).

    Batch triggers spawn a thread targeting this, so the first
    trigger's thread IS the processor and every later trigger's
    thread just enqueues its wake-up and exits — there is never a
    second processor. Each queued row is processed exactly once
    per run.
    """
    global _GIVE_PROCESSOR_ACTIVE
    with _PROCESSOR_GUARD:
        _PENDING_GIVE_IDS.extend(gen_ids)
        if _GIVE_PROCESSOR_ACTIVE:
            return
        _GIVE_PROCESSOR_ACTIVE = True
    processed: set[int] = set()
    try:
        _boot_grace_wait()
        while True:
            with _PROCESSOR_GUARD:
                gen_id = _PRIORITY_GIVE_IDS.popleft() \
                    if _PRIORITY_GIVE_IDS else None
            if gen_id is None:
                gen_id = _claim_next_queued_give_id(processed)
            if gen_id is None:
                with _PROCESSOR_GUARD:
                    gen_id = _PENDING_GIVE_IDS.popleft() \
                        if _PENDING_GIVE_IDS else None
            if gen_id is None:
                return
            _discard_handed_in(gen_id)
            if gen_id in processed:
                continue
            processed.add(gen_id)
            _wait_out_live_render()
            if _run_give_generation_job(gen_id) == "parked":
                # Session unavailable: stand down for this drain.
                # The parked row and everything behind it stay
                # pending for the next trigger / boot / sweep —
                # draining on would just park (or worse, fail)
                # every remaining job against the same dead login.
                print("[give_gen] processor standing down: Sogni "
                      "session unavailable; queue stays pending",
                      flush=True)
                return
    finally:
        with _PROCESSOR_GUARD:
            _GIVE_PROCESSOR_ACTIVE = False
