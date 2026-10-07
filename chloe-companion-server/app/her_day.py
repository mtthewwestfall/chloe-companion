"""Her Day feed generator — Instagram-stories-style daily posts.

Each girl posts 3-5 "moments" per day per user: morning coffee, gym,
cooking, out with friends, getting ready for bed, voice rants, etc.

Generation runs once daily (6 AM cron or on-demand). For each active
user+girl pair:
  1. Pick 3-5 moments from the template pool (varied by day of week).
  2. Gemini writes a caption in the girl's voice/persona (via key pool).
  3. Visual: reuse from her Give library, or text card if none available.
  4. One TTS voice note per day (via existing voice pipeline; skipped when
     voice is disabled).

Posts expire after 24h (expires_at). Old posts are cleaned up on read.
"""
from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from . import config, db, store

# Moment templates: (moment_key, post_type, visual_hint)
# visual_hint: "give" = reuse Give library media, "text" = text card
MOMENTS = {
    "morning_coffee": ("image", "give",
        "morning coffee selfie, just woke up, cozy bedroom"),
    "gym": ("image", "give",
        "gym selfie after workout, sporty, energetic"),
    "cooking": ("image", "give",
        "cooking in the kitchen, casual and cute"),
    "out_and_about": ("image", "give",
        "out with friends, having fun, city background"),
    "getting_ready": ("video", "give",
        "getting ready for the evening, trying on outfits"),
    "bedtime": ("image", "give",
        "getting ready for bed, cozy pajamas, sleepy"),
    "voice_rant": ("voice", "text",
        "voice note rant about her day"),
    "shower_thought": ("text", "text",
        "random thought she had in the shower"),
    "craving": ("text", "text",
        "something she's craving right now"),
}

# Day-of-week moment pools (0=Monday). Vary the vibe by day.
DAY_MOMENTS = {
    0: ["morning_coffee", "gym", "cooking", "bedtime"],
    1: ["morning_coffee", "out_and_about", "voice_rant", "bedtime"],
    2: ["morning_coffee", "gym", "shower_thought", "bedtime"],
    3: ["morning_coffee", "cooking", "craving", "bedtime"],
    4: ["morning_coffee", "gym", "getting_ready", "out_and_about"],
    5: ["morning_coffee", "out_and_about", "getting_ready", "voice_rant"],
    6: ["morning_coffee", "cooking", "shower_thought", "bedtime"],
}


def _generate_caption(girl_name: str, moment_key: str,
                      visual_hint: str) -> str:
    """Generate a caption in the girl's voice via the Gemini key pool."""
    try:
        from . import gemini_keys
        conn = db.connect()
        try:
            key_row = gemini_keys.get_key(conn)
        finally:
            conn.close()
        if not key_row:
            return ""
        import urllib.request
        import json as _json
        prompt = (
            f"You are {girl_name}, a playful girlfriend texting her boyfriend. "
            f"Write a short Instagram-story caption (1-2 sentences, casual, "
            f"flirty, no hashtags) for this moment: {visual_hint}. "
            f"Sound like a real girl, not a bot. No emojis spam (max 1)."
        )
        payload = _json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 100, "temperature": 0.9},
        }).encode()
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={key_row['key_value']}",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read())
        text = (data.get("candidates", [{}])[0]
                .get("content", {}).get("parts", [{}])[0]
                .get("text", "").strip())
        gemini_keys.report_success(db.connect(), key_row["id"])
        return text[:500]
    except Exception as e:  # noqa: BLE001
        print(f"[her_day] caption generation failed: {e}", flush=True)
        return ""


def _fallback_caption(girl_name: str, moment_key: str) -> str:
    """Local fallback captions when Gemini is unavailable."""
    fallbacks = {
        "morning_coffee": f"morning... coffee first, then I'll be human",
        "gym": f"just crushed my workout, feeling amazing",
        "cooking": f"making something yummy, wish you were here to taste it",
        "out_and_about": f"out with the girls, miss you though",
        "getting_ready": f"getting ready... which outfit do you like?",
        "bedtime": f"about to crawl into bed, thinking of you",
        "voice_rant": f"okay I need to vent about my day",
        "shower_thought": f"had the weirdest thought in the shower today",
        "craving": f"I'm craving something so bad right now",
    }
    return fallbacks.get(moment_key, f"thinking of you today")


def generate_for_user_girl(user_id: str, girl_id: str,
                           girl_name: str = "Chloe") -> list[dict[str, Any]]:
    """Generate today's Her Day posts for one user+girl pair.

    Returns the list of created post rows. Skips if already generated today.
    """
    conn = db.connect()
    try:
        store.cleanup_expired_her_day(conn)
        if store.her_day_exists_today(conn, user_id, girl_id):
            return []
        weekday = datetime.now().weekday()
        pool = DAY_MOMENTS.get(weekday, DAY_MOMENTS[0])
        # Pick 3-5 moments, always include morning + bedtime bookends
        picks = ["morning_coffee"]
        middle = [m for m in pool if m not in ("morning_coffee", "bedtime")]
        picks += random.sample(middle, min(len(middle), random.randint(1, 3)))
        picks.append("bedtime")
        # Ensure one voice note per day
        if "voice_rant" not in picks and random.random() < 0.7:
            picks.insert(-1, "voice_rant")

        posts = []
        for moment_key in picks:
            post_type, visual_hint, desc = MOMENTS[moment_key]
            media_url = ""
            voice_url = ""
            if visual_hint == "give":
                media = store.get_random_give_media(conn, girl_id)
                if media:
                    media_url = media.get("url", "")
            caption = _generate_caption(girl_name, moment_key, desc)
            if not caption:
                caption = _fallback_caption(girl_name, moment_key)
            if post_type == "voice":
                try:
                    from . import voice as _voice
                    voice_url = (_voice.maybe_generate_voice_note(
                        caption, user_id) or "")
                except Exception:
                    voice_url = ""
            post = store.save_her_day_post(
                conn, user_id, girl_id,
                post_type=post_type, media_url=media_url,
                voice_url=voice_url, caption=caption)
            posts.append(post)
        return posts
    finally:
        conn.close()


def generate_daily() -> dict[str, Any]:
    """Generate Her Day posts for all active user+girl pairs.

    Called by the 6 AM cron. Iterates users and their selected girls.
    """
    conn = db.connect()
    report: dict[str, Any] = {"generated": 0, "skipped": 0, "errors": []}
    try:
        users = [dict(r) for r in conn.execute(
            "SELECT user_id FROM users").fetchall()]
        for user in users:
            uid = user["user_id"]
            # Get the user's girls (from girl_selection or default to chloe)
            try:
                girls = [dict(r) for r in conn.execute(
                    "SELECT DISTINCT girl_id FROM her_day_posts"
                    " WHERE user_id = ?", (uid,)).fetchall()]
                girl_ids = [g["girl_id"] for g in girls] or ["chloe"]
            except Exception:
                girl_ids = ["chloe"]
            for gid in girl_ids:
                try:
                    posts = generate_for_user_girl(uid, gid)
                    if posts:
                        report["generated"] += len(posts)
                    else:
                        report["skipped"] += 1
                except Exception as e:  # noqa: BLE001
                    report["errors"].append(f"{uid}/{gid}: {e}")
        conn.commit()
    finally:
        conn.close()
    return report
