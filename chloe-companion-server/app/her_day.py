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


def _pool_file_for(participant_ids: list[str], moment_key: str) -> str | None:
    """Newest existing pool render for these participants + moment, ANY
    date, or None.

    Pool files share the deterministic herday naming
    (herday_<sorted pids>_<YYYY-MM-DD>_<moment>.jpg) in GENERATED_DIR.
    Since GENERATED_DIR is persistent (2026-10-15), prior days' renders
    survive and can be reused date-independently for the same
    participants+moment instead of rendering a fresh copy.
    """
    pids = "_".join(sorted(p.strip().lower() for p in participant_ids))
    prefix = f"herday_{pids}_"
    suffix = f"_{moment_key}.jpg"
    try:
        names = os.listdir(config.GENERATED_DIR)
    except OSError:
        return None
    best: str | None = None
    best_mtime = -1.0
    for name in names:
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        date_part = name[len(prefix):len(name) - len(suffix)]
        try:
            datetime.strptime(date_part, "%Y-%m-%d")
        except ValueError:
            continue  # not a dated herday cache file — never pool it
        path = os.path.join(config.GENERATED_DIR, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


def validate_her_day_media_url(conn, url: str) -> str | None:
    """Why `url` may not be attached to a Her Day post, or None if OK.

    Her Day is SFW-only: a URL is acceptable only when it names a file
    this app actually serves from GENERATED_DIR under /generated/ AND
    that file is not registered in girl_media (the give library) under
    any kind. Empty string is allowed (it clears the field).
    """
    if not url:
        return None
    rel = url.split("?", 1)[0]
    if not rel.startswith("/generated/"):
        return "url must be a /generated/ asset served by this app"
    name = rel[len("/generated/"):]
    if not name:
        return "url names no file"
    base = os.path.abspath(config.GENERATED_DIR)
    path = os.path.abspath(os.path.join(base, name))
    if os.path.commonpath([base, path]) != base:
        return "url escapes the generated dir"
    if not os.path.isfile(path):
        return "file not found in the generated pool"
    if os.path.basename(name).startswith("give-upload-"):
        return "give uploads are not Her Day media"
    try:
        rows = conn.execute("SELECT url FROM girl_media").fetchall()
    except Exception:  # noqa: BLE001 — no library table, nothing to match
        rows = []
    candidate = os.path.basename(name)
    for row in rows:
        lib_url = (row[0] if not hasattr(row, "keys") else row["url"]) or ""
        if lib_url == rel or \
                os.path.basename(lib_url.split("?", 1)[0]) == candidate:
            return "url is registered in the give library"
    return None


def _plan_day(girl_id: str, date_str: str,
              all_girls: list[dict[str, Any]]) -> list[tuple]:
    """One moment per girl per day (owner direction, 2026-10-17).

    Returns a single [(moment_key, is_group, partner_ids, partner_names)].
    Seeded by girl_id + date: reproducible, different per girl, different
    per day. Mix: mostly solo visual moments, some group moments, the
    occasional voice rant or text thought.
    """
    seed = int(hashlib.md5(
        f"{girl_id}|{date_str}|herday-v3".encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    others = [g for g in all_girls if g["id"] != girl_id]

    roll = rng.random()
    if others and roll < 0.20:
        mkey = rng.choice(list(GROUP_MOMENTS.keys()))
        n_partners = 1 if rng.random() < 0.7 else min(2, len(others))
        partners = rng.sample(others, n_partners)
        return [(mkey, True,
                 [p["id"] for p in partners],
                 [p["name"] for p in partners])]
    if roll < 0.30:
        return [("voice_rant", False, [], [])]
    if roll < 0.40:
        return [(rng.choice(["shower_thought", "craving"]), False, [], [])]
    solo_pool = ["morning_coffee", "gym", "cooking", "out_and_about",
                 "getting_ready", "bedtime"]
    return [(rng.choice(solo_pool), False, [], [])]


def _build_all_plans(all_girls: list[dict[str, Any]],
                     date_str: str) -> dict[str, list[tuple]]:
    """Build the day's one-moment plans, making group moments symmetric
    WITHOUT giving anyone a second post.

    If A's one moment is a group moment with B, it becomes B's one post
    too (same image, B's own caption) — unless B already kept a group
    moment of her own or was already claimed by an earlier group moment.
    """
    plans = {g["id"]: _plan_day(g["id"], date_str, all_girls)
             for g in all_girls}
    girl_map = {g["id"]: g for g in all_girls}
    claimed: set[str] = set()
    for gid in sorted(plans):
        plan = plans[gid]
        if not plan:
            continue
        (mkey, is_group, pids, _pnames) = plan[0]
        claimed.add(gid)
        if not is_group:
            continue
        for pid in pids:
            if pid in claimed or pid not in plans:
                continue
            if plans[pid] and plans[pid][0][1]:
                continue  # she keeps her own group moment
            plans[pid] = [(mkey, True, [gid], [girl_map[gid]["name"]])]
            claimed.add(pid)
    return plans


def _slot_for(girl_id: str, date_str: str) -> str:
    """This girl's publish slot for the day (UTC text, sqlite format).

    Slots land 12:00-23:59 UTC (morning to evening US Eastern) and are
    derived from girl + date, so the posting order rotates daily instead
    of every post appearing at once in the morning batch. A slot already
    past at generation time simply means the post is live immediately.
    """
    h = int(hashlib.md5(
        f"{girl_id}|{date_str}|slot".encode()).hexdigest()[:8], 16)
    hour = 12 + (h % 12)
    minute = (h >> 8) % 60
    return f"{date_str} {hour:02d}:{minute:02d}:00"


def _slot_expiry(slot: str) -> str:
    """expires_at for a slotted post: 24h after its slot."""
    dt = datetime.strptime(slot, "%Y-%m-%d %H:%M:%S")
    from datetime import timedelta
    return (dt + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")


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


# ---------------------------------------------------------------------------
# Story arcs (2026-10-17): her life runs in 6-day storylines so the feed
# reads like a show she's living, not random moments. Deterministic per
# (girl, date) — every user's copy of her day tells the same story, and
# the morning proactive message (which quotes her caption) teases it.
# ---------------------------------------------------------------------------
STORY_ARCS: list[tuple[str, list[str]]] = [
    ("training for the 5K", [
        "she nervously signed up for a 5K this morning and can't believe she did it",
        "her first sunrise training run — exhausted, proud, already sore",
        "she bought new running shoes and is weirdly obsessed with them",
        "a rainy training run left her soaked and laughing at herself",
        "she beat her personal best today and won't stop talking about it",
        "race day tomorrow — she's nervous, excited, and laying out her outfit",
    ]),
    ("adopting a cat", [
        "she visited the shelter 'just to look' and fell for one cat instantly",
        "she can't stop thinking about the cat and showed everyone its photo",
        "she filled out the adoption papers today — it's really happening",
        "she's cat-proofing her apartment and buying tiny supplies",
        "gotcha day: the cat is home, hiding under the couch, and she is smitten",
        "the cat slept on her pillow last night — she is officially a cat mom",
    ]),
    ("the big gallery show", [
        "a gallery invited her to show her work and she's quietly freaking out",
        "she's picking which pieces make the show and second-guessing everything",
        "framing day — her hands are sore and the studio is chaos",
        "she hung the final piece and stood there staring at the wall",
        "opening night is coming and she has nothing to wear (her words)",
        "the show opened — people came, someone cried at her piece, she floated home",
    ]),
    ("learning guitar", [
        "she bought a secondhand guitar and named it already",
        "her fingertips hurt and her chords sound like a dying robot (her words)",
        "she learned her first full song and played it five times in a row",
        "she's been practicing on the porch every evening this week",
        "she wrote a tiny song of her own and is embarrassed how much she likes it",
        "she played for a friend tonight and didn't mess up the bridge",
    ]),
    ("the cookbook challenge", [
        "she's cooking one new recipe a day this week and documenting the chaos",
        "today's recipe humbled her — smoke alarm involved, dinner survived",
        "she nailed a dish so good she texted everyone she knows about it",
        "grocery run for ingredients she had to google in the aisle",
        "she cooked for a friend and got an actual compliment from a picky eater",
        "final dish of the challenge: she plated it like a restaurant and took 40 photos",
    ]),
    ("planning the road trip", [
        "she started planning a road trip and the map is already covered in pins",
        "she's building the playlist and taking requests very seriously",
        "she found the weird roadside stops — a dinosaur park is non-negotiable",
        "she's packing and repacking the car like it's a puzzle",
        "one sleep left — snacks bought, tank full, butterflies going",
        "she's on the road, windows down, sending photos from every stop",
    ]),
]
_ARC_LEN = 6


def arc_beat(girl_id: str, date_str: str) -> tuple[str, str, int, int] | None:
    """(arc_title, beat, day_number, arc_length) for this girl + date."""
    try:
        from datetime import date as _date
        d = _date.fromisoformat((date_str or "")[:10])
    except Exception:
        return None
    doy = d.timetuple().tm_yday
    cycle, beat_idx = divmod(doy, _ARC_LEN)
    # Main cast gets fixed spread offsets so the girls are never all
    # living the same story on the same day; anyone else hashes in.
    _CAST_OFFSETS = {"chloe": 0, "mara": 1, "isabella": 2, "regan": 3,
                     "brittany": 4, "brooklyn": 5}
    if (girl_id or "") in _CAST_OFFSETS:
        offset = _CAST_OFFSETS[girl_id]
    else:
        import hashlib
        offset = int(hashlib.sha1(
            (girl_id or "").encode()).hexdigest()[:2], 16)
    title, beats = STORY_ARCS[(cycle + offset) % len(STORY_ARCS)]
    return title, beats[beat_idx], beat_idx + 1, _ARC_LEN


def _generate_caption(girl_name: str, moment_key: str, desc: str,
                      partner_names: list[str],
                      cache_key: tuple | None = None,
                      story: str = "") -> str:
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
        story_line = (
            f" Ongoing story in her life: {story} — weave it in naturally "
            f"if it fits the moment." if story else "")
        if partner_names:
            prompt = (
                f"You are {girl_name}, a playful girlfriend texting her boyfriend. "
                f"Write a short Instagram-story caption (1-2 sentences, casual, "
                f"flirty, no hashtags) for this moment: {desc}. "
                f"You're hanging out with {', '.join(partner_names)} — mention them "
                f"naturally. Sound like a real girl, not a bot. No emoji spam (max 1)."
                + story_line
            )
        else:
            prompt = (
                f"You are {girl_name}, a playful girlfriend texting her boyfriend. "
                f"Write a short Instagram-story caption (1-2 sentences, casual, "
                f"flirty, no hashtags) for this moment: {desc}. "
                f"Sound like a real girl, not a bot. No emoji spam (max 1)."
                + story_line
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
            _story = ""
            if not is_group:
                _arc = arc_beat(gid, date_str)
                if _arc:
                    _story = (f"day {_arc[2]} of {_arc[3]} of "
                              f"'{_arc[0]}' — {_arc[1]}")
            caption = _generate_caption(girl_name, moment_key, desc,
                                        pnames, cache_key, story=_story)
            if post_type == "voice":
                try:
                    from . import voice as _voice
                    voice_url = (_voice.maybe_generate_voice_note(
                        caption, user_id, character_id=gid) or "")
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
            # Stagger: the post goes live at this girl's slot today,
            # not all at once with the morning batch.
            try:
                slot = _slot_for(gid, date_str)
                store.set_her_day_post_times(
                    conn, post["id"], slot, _slot_expiry(slot))
            except Exception as e:  # noqa: BLE001
                print(f"[her_day] slot scheduling failed: {e}",
                      flush=True)
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
# order — and fill each image post POOL-FIRST: an existing render of
# the same participants+moment from any earlier day in the persistent
# GENERATED_DIR is reused (copied to today's cache name); only moments
# with no pool file go through the render path (the shared per-day
# render cache — one render per distinct (participants, moment)).
# Voice posts are counted, not filled: voice notes sit behind the
# owner's paid-TTS gate (app/voice.py) and cannot be synthesized here.
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
        "image_posts_filled": 0, "image_posts_filled_from_pool": 0,
        "image_posts_still_empty": 0,
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
                url = ""
                # Pool-first (2026-10-15): GENERATED_DIR is persistent
                # now, so reuse an existing render of this exact
                # participants+moment from any earlier day before
                # paying for a new render. The pool file is copied to
                # today's cache name so the row references today's
                # dated file like a fresh render would.
                pool_src = _pool_file_for(participants, moment_key)
                if pool_src:
                    dest = _herday_image_path(participants, moment_key,
                                               date_str)
                    try:
                        if os.path.abspath(pool_src) != \
                                os.path.abspath(dest):
                            os.makedirs(config.GENERATED_DIR,
                                        exist_ok=True)
                            shutil.copyfile(pool_src, dest)
                        url = "/generated/" + os.path.basename(dest)
                    except OSError as e:  # noqa: BLE001
                        print(f"[her_day] backfill pool copy failed "
                              f"({moment_key}): {e}", flush=True)
                        url = ""
                    if url:
                        report["image_posts_filled_from_pool"] += 1
                if not url:
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
                "image_posts_filled", "image_posts_filled_from_pool",
                "image_posts_still_empty",
                "voice_posts_without_audio"):
        if key in _backfill_state:
            state[key] = _backfill_state[key]
    return state
