"""Give generations for public characters — daily 15s Sogni generations per give stage.

For each public character and each give stage (1..9), a generation is queued daily.
Renders use the saved Sogni login session with duration set to 15 seconds.
Finished generations land as `pending` for admin review on the admin page.
The admin can `approve` (adds the video to girl_media) or `delete` (marks as deleted).
"""
from __future__ import annotations

import asyncio
import os
import threading
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
    1: "lift tease, lifting her shirt playfully, teasing smile",
    2: "shirt off, topless, holding her top, bedroom setting",
    3: "boob grabs, touching and holding her breasts, sensual expression",
    4: "nipple pinch, gently pinching her nipples, passionate smile",
    5: "pants off, wearing only underwear or bottomless, sexy pose",
    6: "plays with herself, touching herself intimately, passionate movement",
    7: "titty fuck, intimate chest scene, close up",
    8: "oral, intimate oral scene, close up",
    9: "sex, passionate intimate scene",
}


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
        d["stage_name"] = STAGE_NAMES.get(d.get("stage", 1), f"stage {d.get('stage')}")
        out.append(d)
    return out


def get_give_generation(conn: Any, gen_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM give_generations WHERE id = ?", (gen_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["stage_name"] = STAGE_NAMES.get(d.get("stage", 1), f"stage {d.get('stage')}")
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


def _run_give_generation_job(gen_id: int) -> None:
    """Worker thread: render 15s Sogni video using the saved Sogni login session."""
    conn = db.connect()
    ref_path = None
    try:
        gen = get_give_generation(conn, gen_id)
        if not gen or gen["status"] not in ("pending", "generating"):
            return

        session = assist.stored_sogni_session(conn)
        if not session:
            print(f"[give_gen] gen #{gen_id} waiting: Sogni login missing", flush=True)
            update_give_generation_status(conn, gen_id, "failed", error="Sogni login missing")
            return

        update_give_generation_status(conn, gen_id, "generating")

        girl = characters.get_character(gen["girl_id"])
        ref_path = assist._fetch_first_frame(girl.get("avatar_url") or "")
        prompt = assist._sogni_plan_prompt(
            girl.get("name") or gen["girl_id"], gen["prompt"]
        )

        outcome, fresh_session = assist._sogni_plan_run(
            session, prompt, ref_path, model=config.SOGNI_VIDEO_MODEL, duration=15
        )
        if fresh_session:
            assist.store_sogni_session(conn, fresh_session)
        if outcome.get("status") == "session_dead":
            assist.clear_sogni_session(conn)

        if outcome.get("status") != "done" or not outcome.get("path"):
            err_msg = str(outcome.get("detail") or outcome.get("status") or "render failed")[:200]
            update_give_generation_status(conn, gen_id, "failed", error=err_msg)
            print(f"[give_gen] gen #{gen_id} failed: {err_msg}", flush=True)
            return

        path = outcome["path"]
        video_url = "/generated/" + os.path.basename(path)
        update_give_generation_status(conn, gen_id, "pending", url=video_url)
        print(f"[give_gen] gen #{gen_id} ready: {video_url} (15s)", flush=True)

    except Exception as e:  # noqa: BLE001
        err_msg = str(e)[:200]
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
    t = threading.Thread(
        target=_run_give_generation_job,
        args=(gen_id,),
        daemon=True,
        name=f"give-gen-{gen_id}",
    )
    t.start()


def trigger_daily_give_generations() -> list[int]:
    """Check and queue daily give generations for all public characters x 9 stages.

    For each public character and for each give stage (1..9), if no generation exists
    for today (day=YYYY-MM-DD), queue a new generation job.
    Returns list of queued give generation IDs.
    """
    conn = db.connect()
    queued_ids: list[int] = []
    try:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Public characters only (no owner_user_id)
        public_chars = [c for c in characters.get_characters() if not c.get("owner_user_id")]

        for char in public_chars:
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

                desc = STAGE_DESCRIPTIONS.get(stage, f"stage {stage} give video")
                gen_id = create_give_generation(conn, gid, stage, desc, today_str, status="pending")
                queued_ids.append(gen_id)

    finally:
        conn.close()

    # Spawn background generation jobs
    if queued_ids:
        t = threading.Thread(
            target=_process_queued_give_generations,
            args=(queued_ids,),
            daemon=True,
            name="daily-give-batch",
        )
        t.start()

    return queued_ids


def _process_queued_give_generations(gen_ids: list[int]) -> None:
    for gen_id in gen_ids:
        _run_give_generation_job(gen_id)
