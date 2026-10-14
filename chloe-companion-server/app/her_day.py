"""Whispr Feed feed generator — Instagram-stories-style daily posts.

SFW-ONLY pipeline (the Give library is never used here — a day-in-the-life
feed cannot show explicit clips).

Every character gets her OWN daily feed, and the app shows ALL characters'
stories in one stories bar (tap a girl's avatar to see her day).

Stories can be SOLO or GROUP moments (2-3 girls together — gym buddies,
coffee dates, nights out). Group moments appear in every participating
girl's feed (shared image, each girl's own caption).

Content plan: deterministic per girl+date (seeded), so each girl gets
different moments each day, each day differs from the last, and plans are
pre-determined/reproducible for the next several days.

Generation (6 AM cron or on-demand), per user x per girl:
  1. Build the day's content plan for every girl (solo + group moments).
  2. Sogni generates an SFW scene image per visual moment (fully clothed,
     everyday activity, non-sexual) through the shared plan session
     (assist.stored_sogni_session — subscription billing, the same path
     as the plan renders; the dashboard API key is never used here).
     Images cached per participants+day+moment on disk — all users
     share one generation. Any failure falls back to a styled text
     card.
  3. Caption via the centralized Gemini key manager PROXY
     (POST {PUBLIC_APP_URL}/v1/chat/completions, Bearer GEMINI_PROXY_TOKEN)
     — free keys first, never the paid key directly.
  4. One TTS voice note per girl per day.

Posts expire after 24h (expires_at). Old posts are cleaned up on read.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import random
import shutil
import threading
from datetime import datetime
from typing import Any

from . import config, db, store

# Solo visual moments: (post_type, sfw_scene, caption_desc)
SOLO_MOMENTS = {
    "morning_coffee": ("image",
        "cozy selfie holding a coffee mug in a sunlit bedroom, wearing a modest sweatshirt",
        "morning coffee selfie, just woke up, cozy bedroom"),
    "gym": ("image",
        "selfie at the gym after a workout, wearing modest athletic wear, leggings and a t-shirt",
        "gym selfie after workout, sporty, energetic"),
    "cooking": ("image",
        "cooking in a bright home kitchen, wearing a casual top with an apron",
        "cooking in the kitchen, casual and cute"),
    "out_and_about": ("image",
        "out in the city with friends, wearing a stylish casual daytime outfit",
        "out with friends, having fun, city background"),
    "getting_ready": ("image",
        "mirror selfie trying on a cute daytime outfit",
        "getting ready for the day, trying on outfits"),
    "bedtime": ("image",
        "cozy selfie in pajamas in a warm bedroom, sleepy goodnight vibe",
        "getting ready for bed, cozy pajamas, sleepy"),
}

# Group visual moments: (post_type, sfw_scene, caption_desc_template).
# {partners} is filled with the other girls' names.
GROUP_MOMENTS = {
    "gym_buddies": ("image",
        "working out together at the gym, spotting each other on weights, all in modest athletic wear",
        "gym with {partners}, workout buddies"),
    "coffee_date": ("image",
        "having coffee together at a cozy cafe, chatting and laughing",
        "coffee date with {partners}"),
    "night_out": ("image",
        "out together in the city at night, having fun, stylish evening outfits",
        "night out with {partners}"),
    "cooking_together": ("image",
        "cooking together in a bright home kitchen, laughing and having fun",
        "cooking with {partners}"),
}

# Non-visual moments (solo only): (post_type, caption_desc)
TEXT_MOMENTS = {
    "voice_rant": ("voice", "voice note rant about her day"),
    "shower_thought": ("text", "random thought she had in the shower"),
    "craving": ("text", "something she's craving right now"),
}

# Hard SFW tail appended to every Whispr Feed image prompt.
SFW_TAIL = ("photorealistic, fully clothed, everyday normal activity, "
            "non-sexual, wholesome, like a normal Instagram story, "
            "clean high-quality photo, natural light, coherent background")

# In-memory caption cache for batch runs: (girl_id, date, moment, partners)
_caption_cache: dict[tuple, str] = {}


def _sfw_prompt(names: list[str], scene: str) -> str:
    """Build an explicitly SFW image prompt. names[0] is the primary girl."""
    name_str = " and ".join(names)
    return f"Photorealistic {name_str}, {scene}, {SFW_TAIL}"


def _herday_image_path(participant_ids: list[str], moment_key: str,
                       date_str: str) -> str:
    """Daily cache path — all users share one generation per participants."""
    pids = "_".join(sorted(p.strip().lower() for p in participant_ids))
    fname = f"herday_{pids}_{date_str}_{moment_key}.jpg"
    return os.path.join(config.GENERATED_DIR, fname)


def _plan_day(girl_id: str, date_str: str,
              all_girls: list[dict[str, Any]]) -> list[tuple]:
    """Deterministic daily content plan for one girl.

    Returns [(moment_key, is_group, partner_ids, partner_names)].
    Seeded by girl_id + date: reproducible, different per girl, different
    per day. Mixes solo and group moments.
    """
    seed = int(hashlib.md5(
        f"{girl_id}|{date_str}|herday-v2".encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    others = [g for g in all_girls if g["id"] != girl_id]

    plan: list[tuple] = []
    # Bookend: morning coffee (solo)
    plan.append(("morning_coffee", False, [], []))

    # Middle: 1-3 moments, mix solo + group
    solo_pool = ["gym", "cooking", "out_and_about", "getting_ready"]
    group_pool = list(GROUP_MOMENTS.keys())
    for _ in range(rng.randint(1, 3)):
        if others and rng.random() < 0.45:
            mkey = rng.choice(group_pool)
            n_partners = 1 if rng.random() < 0.7 else min(2, len(others))
            partners = rng.sample(others, n_partners)
            plan.append((mkey, True,
                         [p["id"] for p in partners],
                         [p["name"] for p in partners]))
        else:
            plan.append((rng.choice(solo_pool), False, [], []))

    # Bookend: bedtime (solo)
    plan.append(("bedtime", False, [], []))

    # Voice note most days
    if rng.random() < 0.7:
        plan.insert(-1, ("voice_rant", False, [], []))
    # Sometimes a text moment
    if rng.random() < 0.4:
        plan.insert(-1, (rng.choice(["shower_thought", "craving"]),
                         False, [], []))
    return plan


def _build_all_plans(all_girls: list[dict[str, Any]],
                     date_str: str) -> dict[str, list[tuple]]:
    """Build plans for all girls, making group moments symmetric.

    If A's plan has a group moment with B, it also appears in B's feed
    (same image, B's own caption).
    """
    plans = {g["id"]: _plan_day(g["id"], date_str, all_girls)
             for g in all_girls}
    girl_map = {g["id"]: g for g in all_girls}
    for gid, plan in list(plans.items()):
        for (mkey, is_group, pids, _pnames) in plan:
            if not is_group:
                continue
            for pid in pids:
                if pid not in plans:
                    continue
                already = any(pm == mkey and gid in ppids
                              for (pm, pig, ppids, _) in plans[pid])
                if not already:
                    plans[pid].insert(
                        -1, (mkey, True, [gid], [girl_map[gid]["name"]]))
    return plans


def _plan_session() -> dict | None:
    """The shared Sogni plan session (assist_secrets row
    "sogni_plan_session", env seed as fallback), or None.

    This is the session every working render in the backend uses
    (gives, chat plan renders). The dashboard API key is NOT a usable
    session here: plan renders run on the subscription web plan
    only, and stored_sogni_session's API-key fallback is treated as
    no session (same stance as _sogni_plan_create_project_sync).
    """
    from . import assist

    conn = db.connect()
    try:
        session = assist.stored_sogni_session(conn)
    finally:
        conn.close()
    if not session or session.get("api_key"):
        return None
    return session


def _render_image_plan(session: dict, prompt: str,
                       ref_path: str | None) -> dict:
    """One plan-billed image render via assist's locked seam (sync).

    assist._locked_render serializes renders on the shared login
    (parallel renders on one refresh token end in "The account
    changed"), re-reads the newest stored pair, persists the
    rotated pair afterwards, and heals a session_dead outcome
    once in-seam (resolve + re-run, marked "seam_healed"). Returns
    assist's outcome dict ({"status", "path", "detail", ...}).
    """
    from . import assist

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        outcome, _fresh = assist._locked_render(
            loop, session,
            lambda s: assist._sogni_plan_render(
                s, prompt, ref_path,
                model=config.SOGNI_IMAGE_MODEL, kind="image"))
        return outcome
    finally:
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass


def _generate_sfw_image(names: list[str], avatar_url: str,
                        participant_ids: list[str], moment_key: str,
                        scene: str, date_str: str) -> str:
    """Generate (or reuse today's cached) SFW image via the plan session.

    Returns the /generated/... url, or "" on any failure — the caller
    renders a styled text card instead.
    """
    dest = _herday_image_path(participant_ids, moment_key, date_str)
    if os.path.exists(dest):
        return "/generated/" + os.path.basename(dest)
    try:
        session = _plan_session()
    except Exception as e:  # noqa: BLE001
        print(f"[her_day] plan session lookup failed: {e}", flush=True)
        return ""
    if not session:
        print("[her_day] no Sogni plan session, text card fallback",
              flush=True)
        return ""
    ref_path = None
    try:
        from .media_gen import _fetch_reference

        ref_path = _fetch_reference(avatar_url or "")
        outcome = _render_image_plan(
            session, _sfw_prompt(names, scene), ref_path)
        if outcome.get("status") == "session_dead" \
                and not outcome.get("seam_healed"):
            # Non-destructive recovery, same as give_gen: re-read the
            # newest stored pair and retry once with it when another
            # feature already refreshed the shared login; assist
            # clears the pair (and tombstones the env seed) only when
            # it is confirmed dead and a re-login has also failed.
            # Skipped when the seam already healed and re-ran this
            # render (outcome["seam_healed"] — _render_image_plan goes
            # through assist._locked_render, which owns the heal).
            recovered = None
            try:
                from . import assist

                conn = db.connect()
                try:
                    recovered = assist.resolve_dead_sogni_session(
                        conn, session, caller="her_day")
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001
                recovered = None
            if recovered:
                outcome = _render_image_plan(
                    recovered, _sfw_prompt(names, scene), ref_path)
        if outcome.get("status") != "done" or not outcome.get("path"):
            print(f"[her_day] sfw image failed ({moment_key}): "
                  f"{outcome.get('status')}: {outcome.get('detail')}",
                  flush=True)
            return ""
        os.makedirs(config.GENERATED_DIR, exist_ok=True)
        shutil.copyfile(outcome["path"], dest)
        return "/generated/" + os.path.basename(dest)
    except Exception as e:  # noqa: BLE001
        print(f"[her_day] sfw image failed ({moment_key}): {e}", flush=True)
        return ""
    finally:
        if ref_path:
            try:
                os.unlink(ref_path)
            except OSError:
                pass


def _generate_caption(girl_name: str, moment_key: str, desc: str,
                      partner_names: list[str],
                      cache_key: tuple | None = None) -> str:
    """Caption via the centralized Gemini key manager PROXY (free keys first).

    POST {PUBLIC_APP_URL}/v1/chat/completions with Bearer GEMINI_PROXY_TOKEN.
    """
    if cache_key and cache_key in _caption_cache:
        return _caption_cache[cache_key]

    import json as _json
    import urllib.request

    token = os.environ.get("GEMINI_PROXY_TOKEN", "")
    text = ""
    if token:
        if partner_names:
            prompt = (
                f"You are {girl_name}, a playful girlfriend texting her boyfriend. "
                f"Write a short Instagram-story caption (1-2 sentences, casual, "
                f"flirty, no hashtags) for this moment: {desc}. "
                f"You're hanging out with {', '.join(partner_names)} — mention them "
                f"naturally. Sound like a real girl, not a bot. No emoji spam (max 1)."
            )
        else:
            prompt = (
                f"You are {girl_name}, a playful girlfriend texting her boyfriend. "
                f"Write a short Instagram-story caption (1-2 sentences, casual, "
                f"flirty, no hashtags) for this moment: {desc}. "
                f"Sound like a real girl, not a bot. No emoji spam (max 1)."
            )
        payload = _json.dumps({
            "model": "gemini-3.1-flash-lite",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.9,
            "max_tokens": 100,
        }).encode()
        url = config.PUBLIC_APP_URL.rstrip("/") + "/v1/chat/completions"
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = _json.loads(resp.read())
            text = (data.get("choices", [{}])[0]
                    .get("message", {}).get("content", "").strip())[:500]
        except Exception as e:  # noqa: BLE001
            print(f"[her_day] proxy caption failed: {e}", flush=True)
    if not text:
        text = _fallback_caption(girl_name, moment_key, partner_names)
    if cache_key:
        _caption_cache[cache_key] = text
    return text


def _fallback_caption(girl_name: str, moment_key: str,
                      partner_names: list[str]) -> str:
    """Local fallback captions when the proxy is unavailable."""
    partner_bit = (f" with {', '.join(partner_names)}"
                   if partner_names else "")
    fallbacks = {
        "morning_coffee": f"morning... coffee first, then I'll be human",
        "gym": f"just crushed my workout, feeling amazing",
        "cooking": f"making something yummy, wish you were here to taste it",
        "out_and_about": f"out with the girls, miss you though",
        "getting_ready": f"getting ready... which outfit do you like?",
        "bedtime": f"about to crawl into bed, thinking of you",
        "gym_buddies": f"gym with{partner_bit}, she pushed me so hard",
        "coffee_date": f"coffee date{partner_bit}, she stole my fries lol",
        "night_out": f"out tonight{partner_bit}, living our best lives",
        "cooking_together": f"cooking{partner_bit}, taste tester needed",
        "voice_rant": f"okay I need to vent about my day",
        "shower_thought": f"had the weirdest thought in the shower today",
        "craving": f"I'm craving something so bad right now",
    }
    return fallbacks.get(moment_key, f"thinking of you today")


def _get_all_girls() -> list[dict[str, Any]]:
    """All characters for Whispr Feed: [{id, name, avatar_url}]."""
    try:
        from . import characters
        chars = characters.get_characters()
        return [{"id": c["id"], "name": c.get("name") or "Chloe",
                 "avatar_url": c.get("avatar_url") or ""} for c in chars]
    except Exception:
        return [{"id": "chloe", "name": "Chloe", "avatar_url": ""}]


def _moment_def(moment_key: str):
    """Return (post_type, sfw_scene, caption_desc_template) for a moment."""
    if moment_key in SOLO_MOMENTS:
        return SOLO_MOMENTS[moment_key]
    if moment_key in GROUP_MOMENTS:
        return GROUP_MOMENTS[moment_key]
    post_type, desc = TEXT_MOMENTS[moment_key]
    return (post_type, None, desc)


def generate_for_user_girl(user_id: str, girl_id: str,
                           girl_name: str = "Chloe",
                           plan: list[tuple] | None = None,
                           girl_map: dict[str, dict] | None = None,
                           date_str: str | None = None) -> list[dict[str, Any]]:
    """Generate today's Whispr Feed posts for one user+girl pair.

    plan: [(moment_key, is_group, partner_ids, partner_names)] from
    _build_all_plans. If None, builds a solo-only plan for this girl.
    """
    conn = db.connect()
    try:
        store.cleanup_expired_her_day(conn)
        if store.her_day_exists_today(conn, user_id, girl_id):
            return []
        gid = (girl_id or "chloe").strip().lower() or "chloe"
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        girl_map = girl_map or {}

        # Resolve this girl's info
        ginfo = girl_map.get(gid) or {"id": gid, "name": girl_name,
                                      "avatar_url": ""}
        girl_name = ginfo.get("name") or girl_name
        avatar_url = ginfo.get("avatar_url") or ""

        if plan is None:
            all_girls = _get_all_girls()
            plan = _plan_day(gid, date_str, all_girls)

        posts = []
        for (moment_key, is_group, pids, pnames) in plan:
            post_type, sfw_scene, desc_tpl = _moment_def(moment_key)
            desc = (desc_tpl.format(partners=", ".join(pnames))
                    if is_group and "{partners}" in desc_tpl else desc_tpl)
            media_url = ""
            voice_url = ""

            if post_type == "image" and sfw_scene:
                if is_group:
                    names = [girl_name] + pnames
                    participants = [gid] + pids
                else:
                    names = [girl_name]
                    participants = [gid]
                media_url = _generate_sfw_image(
                    names, avatar_url, participants,
                    moment_key, sfw_scene, date_str)
                # Empty media_url -> frontend renders a styled text card.

            cache_key = (gid, date_str, moment_key, tuple(sorted(pids)))
            caption = _generate_caption(girl_name, moment_key, desc,
                                        pnames, cache_key)
            if post_type == "voice":
                try:
                    from . import voice as _voice
                    voice_url = (_voice.maybe_generate_voice_note(
                        caption, user_id) or "")
                    if not voice_url:
                        # Voice notes are an owner-approved paid-TTS
                        # feature (app/voice.py is stubbed until then);
                        # log it so the empty voice_url is never a
                        # silent failure again.
                        print("[her_day] voice note unavailable "
                              "(voice disabled/stubbed); voice post "
                              "saved without audio", flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"[her_day] voice note failed: {e}", flush=True)
                    voice_url = ""
            post = store.save_her_day_post(
                conn, user_id, gid,
                post_type=post_type, media_url=media_url,
                voice_url=voice_url, caption=caption)
            posts.append(post)
        return posts
    finally:
        conn.close()


def generate_for_all_girls(user_id: str) -> int:
    """Generate today's Whispr Feed for EVERY girl for one user.

    Returns the total number of posts created.
    """
    all_girls = _get_all_girls()
    date_str = datetime.now().strftime("%Y-%m-%d")
    plans = _build_all_plans(all_girls, date_str)
    girl_map = {g["id"]: g for g in all_girls}
    total = 0
    for g in all_girls:
        posts = generate_for_user_girl(
            user_id, g["id"], g["name"],
            plan=plans.get(g["id"], []),
            girl_map=girl_map,
            date_str=date_str,
        )
        total += len(posts)
    return total


def generate_daily() -> dict[str, Any]:
    """Generate Whispr Feed posts for all users x all girls.

    Called by the 6 AM cron.
    """
    conn = db.connect()
    try:
        users = [r["user_id"] for r in conn.execute(
            "SELECT user_id FROM users").fetchall()]
    finally:
        conn.close()
    report: dict[str, Any] = {"generated": 0, "skipped": 0, "errors": []}
    for uid in users:
        try:
            n = generate_for_all_girls(uid)
            if n:
                report["generated"] += n
            else:
                report["skipped"] += 1
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"{uid}: {e}")
    return report


# ---------------------------------------------------------------------------
# Backfill: repair today's already-generated posts in place.
#
# A day whose generation ran while image rendering was broken is left
# with caption-only rows (media_url = ''). Regeneration is blocked by
# her_day_exists_today, so instead pair today's rows back to the
# deterministic plan — rows for a (user, girl) were inserted in plan
# order — and fill each image post from the shared per-day render
# cache. The ~2k user rows therefore cost one render per distinct
# (participants, moment). Voice posts are counted, not filled: voice
# notes sit behind the owner's paid-TTS gate (app/voice.py) and cannot
# be synthesized here.
# ---------------------------------------------------------------------------

_backfill_lock = threading.Lock()
_backfill_state: dict[str, Any] = {"running": False, "report": None,
                                   "error": None, "started_at": None}


def backfill_today_media(date_str: str | None = None,
                         progress: dict[str, Any] | None = None
                         ) -> dict[str, Any]:
    """Fill today's empty Whispr Feed image posts in place.

    Returns counts: pairs processed/skipped, image posts filled/still
    empty, and voice posts left without audio (owner-gated TTS).
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    all_girls = _get_all_girls()
    plans = _build_all_plans(all_girls, date_str)
    girl_map = {g["id"]: g for g in all_girls}
    report: dict[str, Any] = {
        "date": date_str, "pairs_processed": 0, "pairs_skipped": 0,
        "image_posts_filled": 0, "image_posts_still_empty": 0,
        "voice_posts_without_audio": 0,
    }

    def _progress() -> None:
        if progress is not None:
            progress.update(report)

    conn = db.connect()
    try:
        pairs = conn.execute(
            "SELECT DISTINCT user_id, girl_id FROM her_day_posts"
            " WHERE date(created_at) = date('now')").fetchall()
        for pair in pairs:
            uid, gid = pair["user_id"], pair["girl_id"]
            plan = plans.get(gid)
            if not plan:
                report["pairs_skipped"] += 1
                _progress()
                continue
            rows = [dict(r) for r in conn.execute(
                "SELECT id, post_type, media_url, voice_url"
                " FROM her_day_posts WHERE user_id = ? AND girl_id = ?"
                " AND date(created_at) = date('now') ORDER BY id",
                (uid, gid)).fetchall()]
            if len(rows) != len(plan):
                # Cannot pair rows to moments reliably — leave them.
                report["pairs_skipped"] += 1
                _progress()
                continue
            report["pairs_processed"] += 1
            ginfo = girl_map.get(gid) or {"id": gid, "name": gid,
                                           "avatar_url": ""}
            for row, (moment_key, is_group, pids, pnames) in zip(
                    rows, plan):
                if row["post_type"] == "voice":
                    if not row["voice_url"]:
                        report["voice_posts_without_audio"] += 1
                    continue
                if row["post_type"] != "image" or row["media_url"]:
                    continue
                _ptype, sfw_scene, _desc = _moment_def(moment_key)
                if not sfw_scene:
                    continue
                if is_group:
                    names = [(ginfo.get("name") or gid)] + pnames
                    participants = [gid] + pids
                else:
                    names = [ginfo.get("name") or gid]
                    participants = [gid]
                url = _generate_sfw_image(
                    names, ginfo.get("avatar_url") or "", participants,
                    moment_key, sfw_scene, date_str)
                if url:
                    conn.execute(
                        "UPDATE her_day_posts SET media_url = ?"
                        " WHERE id = ?", (url, row["id"]))
                    conn.commit()
                    report["image_posts_filled"] += 1
                else:
                    report["image_posts_still_empty"] += 1
            _progress()
        return report
    finally:
        conn.close()


def start_backfill_today_media() -> dict[str, Any]:
    """Start the backfill on a daemon thread (renders are serialized
    and can take a while); poll backfill_status() for the report."""
    with _backfill_lock:
        if _backfill_state.get("running"):
            return {"ok": True, "started": False, "already_running": True}
        _backfill_state.update({
            "running": True, "report": None, "error": None,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

    def _run() -> None:
        try:
            report = backfill_today_media(progress=_backfill_state)
            _backfill_state["report"] = report
        except Exception as e:  # noqa: BLE001
            _backfill_state["error"] = str(e)[:300]
            print(f"[her_day] backfill failed: {e}", flush=True)
        finally:
            _backfill_state["running"] = False

    threading.Thread(target=_run, daemon=True,
                     name="herday-backfill").start()
    return {"ok": True, "started": True}


def backfill_status() -> dict[str, Any]:
    """Current backfill state (live counters while running)."""
    state: dict[str, Any] = {"ok": True,
                             "running": bool(_backfill_state.get("running")),
                             "started_at": _backfill_state.get("started_at"),
                             "error": _backfill_state.get("error")}
    report = _backfill_state.get("report")
    if report is not None:
        state["report"] = report
    # Live counters (the running backfill merges them into the state).
    for key in ("date", "pairs_processed", "pairs_skipped",
                "image_posts_filled", "image_posts_still_empty",
                "voice_posts_without_audio"):
        if key in _backfill_state:
            state[key] = _backfill_state[key]
    return state
