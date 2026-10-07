"""Her Day feed generator — Instagram-stories-style daily posts.

SFW-ONLY pipeline (the Give library is never used here — a day-in-the-life
feed cannot show explicit clips):

Each girl posts 3-5 "moments" per day per user: morning coffee, gym,
cooking, out with friends, getting ready, bedtime, voice rants, etc.

Generation runs once daily (6 AM cron or on-demand). For each active
user+girl pair:
  1. Pick 3-5 moments from the template pool (varied by day of week).
  2. Sogni generates an SFW scene image per visual moment (fully clothed,
     everyday normal activity, non-sexual, Instagram-story style), with the
     girl's avatar as a face-lock reference. Images are cached per
     girl+day+moment so all users share one generation. Any failure
     (missing key, content block, timeout) falls back to a styled text
     card — explicit content can never appear here.
  3. Caption via the centralized Gemini key manager PROXY
     (POST {PUBLIC_APP_URL}/v1/chat/completions, Bearer GEMINI_PROXY_TOKEN)
     — free keys first, never the paid key directly.
  4. One TTS voice note per day (via existing voice pipeline; skipped when
     voice is disabled).

Posts expire after 24h (expires_at). Old posts are cleaned up on read.
"""
from __future__ import annotations

import asyncio
import os
import random
from datetime import datetime
from typing import Any

from . import config, db, store

# Moment templates: (post_type, sfw_scene, caption_desc)
# sfw_scene: scene description fed to the Sogni prompt (None for
# non-visual moments). Every visual moment is an SFW everyday scene.
MOMENTS = {
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
    "voice_rant": ("voice", None,
        "voice note rant about her day"),
    "shower_thought": ("text", None,
        "random thought she had in the shower"),
    "craving": ("text", None,
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

# Hard SFW tail appended to every Her Day image prompt. The Give library
# is never a source here — explicit content cannot reach this feed.
SFW_TAIL = ("photorealistic, fully clothed, everyday normal activity, "
            "non-sexual, wholesome, like a normal Instagram story, "
            "clean high-quality photo, natural light, coherent background")


def _sfw_prompt(girl_name: str, scene: str) -> str:
    """Build an explicitly SFW image prompt for a Her Day moment."""
    return f"Photorealistic {girl_name}, {scene}, {SFW_TAIL}"


def _herday_image_path(girl_id: str, moment_key: str) -> str:
    """Daily cache path so all users share one generation per girl/moment."""
    day = datetime.now().strftime("%Y-%m-%d")
    gid = (girl_id or "chloe").strip().lower() or "chloe"
    fname = f"herday_{gid}_{day}_{moment_key}.jpg"
    return os.path.join(config.GENERATED_DIR, fname)


async def _generate_sfw_image_async(girl_name: str, avatar_url: str,
                                    girl_id: str, moment_key: str,
                                    scene: str, dest: str) -> str:
    """Run one SFW Sogni image job. Returns the /generated/... url."""
    from sogni_client import SogniClient
    from .media_gen import (
        _download,
        _fetch_reference,
        _patch_websocket_quirk,
        _ws_factory,
    )

    _patch_websocket_quirk()
    ref_path = _fetch_reference(avatar_url or "")
    params: dict[str, Any] = {
        "positivePrompt": _sfw_prompt(girl_name, scene),
        "numberOfMedia": 1,
        "type": "image",
        "modelId": config.SOGNI_IMAGE_MODEL,
        "width": 768,
        "height": 1344,
    }
    if ref_path:
        params["referenceImage"] = ref_path
    try:
        client = await SogniClient.create(
            api_key=config.SOGNI_API_KEY,
            app_id="companion-her-day",
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
                        print(f"[her_day] mint failed for {img_id}: {e}",
                              flush=True)
            if not urls:
                raise RuntimeError("no result urls from Sogni")
            os.makedirs(config.GENERATED_DIR, exist_ok=True)
            _download(urls[0], dest)
            return "/generated/" + os.path.basename(dest)
        finally:
            await client.aclose()
    finally:
        if ref_path:
            try:
                os.unlink(ref_path)
            except OSError:
                pass


def _generate_sfw_image(girl_name: str, avatar_url: str, girl_id: str,
                        moment_key: str, scene: str) -> str:
    """Generate (or reuse today's cached) SFW image for a Her Day moment.

    Returns the /generated/... url, or "" on any failure — the caller
    renders a styled text card instead. Explicit content can never appear.
    """
    dest = _herday_image_path(girl_id, moment_key)
    if os.path.exists(dest):
        return "/generated/" + os.path.basename(dest)
    if not config.SOGNI_API_KEY:
        print("[her_day] SOGNI_API_KEY not set, text card fallback",
              flush=True)
        return ""
    try:
        return asyncio.run(_generate_sfw_image_async(
            girl_name, avatar_url, girl_id, moment_key, scene, dest))
    except Exception as e:  # noqa: BLE001
        # Content blocks ("Sensitive content detected"), timeouts, etc.
        print(f"[her_day] sfw image failed ({moment_key}): {e}", flush=True)
        return ""


def _generate_caption(girl_name: str, moment_key: str,
                      desc: str) -> str:
    """Caption via the centralized Gemini key manager PROXY.

    POST {PUBLIC_APP_URL}/v1/chat/completions with Bearer GEMINI_PROXY_TOKEN.
    The proxy routes free keys first with failover — the paid key is never
    used directly here.
    """
    import json as _json
    import urllib.request

    token = os.environ.get("GEMINI_PROXY_TOKEN", "")
    if not token:
        return ""
    prompt = (
        f"You are {girl_name}, a playful girlfriend texting her boyfriend. "
        f"Write a short Instagram-story caption (1-2 sentences, casual, "
        f"flirty, no hashtags) for this moment: {desc}. "
        f"Sound like a real girl, not a bot. No emojis spam (max 1)."
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
                .get("message", {}).get("content", "").strip())
        return text[:500]
    except Exception as e:  # noqa: BLE001
        print(f"[her_day] proxy caption failed: {e}", flush=True)
        return ""


def _fallback_caption(girl_name: str, moment_key: str) -> str:
    """Local fallback captions when the proxy is unavailable."""
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
    Visuals are SFW Sogni generations (cached per girl+day+moment); any
    failure falls back to a text card. The Give library is never used.
    """
    conn = db.connect()
    try:
        store.cleanup_expired_her_day(conn)
        if store.her_day_exists_today(conn, user_id, girl_id):
            return []
        gid = (girl_id or "chloe").strip().lower() or "chloe"
        avatar_url = ""
        try:
            from . import characters
            character = characters.get_character(gid)
            avatar_url = character.get("avatar_url") or ""
            girl_name = character.get("name") or girl_name
        except Exception:
            pass
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
            post_type, sfw_scene, desc = MOMENTS[moment_key]
            media_url = ""
            voice_url = ""
            if post_type == "image" and sfw_scene:
                media_url = _generate_sfw_image(
                    girl_name, avatar_url, gid, moment_key, sfw_scene)
                # Empty media_url -> frontend renders a styled text card.
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
                conn, user_id, gid,
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
