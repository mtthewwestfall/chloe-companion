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

Generation (6 AM cron or on-demand), ONCE per girl per day (shared day-set):
  1. Build the day's content plan for every girl (solo + group moments).
  2. Sogni generates an SFW scene image per visual moment (fully clothed,
     everyday activity, non-sexual). Images cached per participants+day+
     moment on disk — all users share one generation. Any failure falls
     back to a styled text card.
  3. Caption via the centralized Gemini key manager PROXY
     (POST {PUBLIC_APP_URL}/v1/chat/completions, Bearer GEMINI_PROXY_TOKEN)
     — free keys first, never the paid key directly.
  4. One TTS voice note per girl per day.

The finished posts are stored once under store.HER_DAY_SHARED_USER and
every user's feed reads them from there (lazy attach; per-user state is
only lightweight seen rows in her_day_seen). The daily run never iterates
user accounts, so probe/test accounts are never generated or written for.

Owner-scoped personal sets (2026-10-09): user-created characters (enabled
companion-local characters with an owner_user_id) get a day-set too —
stored under their OWNER's user_id, never under the shared sentinel, so
they surface only in that owner's feed. Their moments are picked from a
profile palette derived from the character's persona text (a CEO's day is
meetings and city light; a farm character's is porches and fence lines),
captions are written in the character's own voice from that same persona,
and group moments never apply (a personal character's day is solo).

COST NOTE: Her Day images (roster and personal alike) are generated via
the Sogni API-key client (config.SOGNI_API_KEY, _generate_sfw_image_async)
— that is the credits-based API route, NOT the owner's $20 Unlimited web
plan. Every personal character adds roughly one day-set of image
generations per day to that route; the caps below bound the fan-out.

Posts expire after 24h (expires_at). Old posts are cleaned up on read.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
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


async def _generate_sfw_image_async(names: list[str], avatar_url: str,
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
        "positivePrompt": _sfw_prompt(names, scene),
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


def _generate_sfw_image(names: list[str], avatar_url: str,
                        participant_ids: list[str], moment_key: str,
                        scene: str, date_str: str) -> str:
    """Generate (or reuse today's cached) SFW image.

    Returns the /generated/... url, or "" on any failure — the caller
    renders a styled text card instead.
    """
    dest = _herday_image_path(participant_ids, moment_key, date_str)
    if os.path.exists(dest):
        return "/generated/" + os.path.basename(dest)
    if not config.SOGNI_API_KEY:
        print("[her_day] SOGNI_API_KEY not set, text card fallback",
              flush=True)
        return ""
    try:
        return asyncio.run(_generate_sfw_image_async(
            names, avatar_url, scene, dest))
    except Exception as e:  # noqa: BLE001
        print(f"[her_day] sfw image failed ({moment_key}): {e}", flush=True)
        return ""


def _caption_via_proxy(prompt: str) -> str:
    """One caption completion via the centralized Gemini key manager PROXY
    (free keys first). Returns "" on any failure — callers fall back."""
    import json as _json
    import urllib.request

    token = os.environ.get("GEMINI_PROXY_TOKEN", "")
    if not token:
        return ""
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
        return (data.get("choices", [{}])[0]
                .get("message", {}).get("content", "").strip())[:500]
    except Exception as e:  # noqa: BLE001
        print(f"[her_day] proxy caption failed: {e}", flush=True)
        return ""


def _generate_caption(girl_name: str, moment_key: str, desc: str,
                      partner_names: list[str],
                      cache_key: tuple | None = None) -> str:
    """Caption via the centralized Gemini key manager PROXY (free keys first).

    POST {PUBLIC_APP_URL}/v1/chat/completions with Bearer GEMINI_PROXY_TOKEN.
    """
    if cache_key and cache_key in _caption_cache:
        return _caption_cache[cache_key]

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
    text = _caption_via_proxy(prompt)
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


# ---------------------------------------------------------------------------
# Owner-scoped personal day-sets for user-created characters.
#
# A user-created character is an enabled companion-local character with an
# owner_user_id (local_characters). Their day-set is stored under the
# owner's user_id — never under HER_DAY_SHARED_USER — so the existing feed
# read path returns it only to that owner. Moments come from a profile
# palette scored against the character's persona text; scenes stay inside
# the SFW moment families (the SFW tail is appended at prompt build, as
# always) but say what THAT character's day actually looks like.
# ---------------------------------------------------------------------------

# Deterministic scoring wins; earlier palettes break ties. "pool" orders
# the solo moment keys the day planner prefers for this kind of life.
PROFILE_PALETTES: list[dict[str, Any]] = [
    {
        "key": "executive",
        "keywords": ("ceo", "executive", "business", "corporate", "office",
                     "boardroom", "meeting", "lawyer", "banker", "investor",
                     "entrepreneur", "founder", "boss", "suit", "agency",
                     "marketing", "sales director", "realtor", "broker"),
        "pool": ["out_and_about", "getting_ready", "gym", "cooking"],
        "moments": {
            "morning_coffee": (
                "early coffee in a bright high-rise apartment before work, "
                "smart blazer, laptop open on the counter",
                "coffee before the first meeting, already in work mode"),
            "out_and_about": (
                "walking between meetings downtown, tailored coat, phone "
                "in hand, city towers behind",
                "between meetings downtown, city moving fast around me"),
            "getting_ready": (
                "mirror check in a tailored outfit before heading out, "
                "confident and put-together",
                "last mirror check before I run the day"),
            "bedtime": (
                "winding down in a high-rise apartment at night, city "
                "lights through the window, cozy sweater",
                "city lights, work brain finally switching off"),
        },
        "fallback": "long day, my kind of day",
    },
    {
        "key": "farm",
        "keywords": ("farm", "ranch", "barn", "horse", "field", "crops",
                     "harvest", "cowboy", "cowgirl", "country", "homestead",
                     "tractor", "livestock", "garden", "ranch hand"),
        "pool": ["out_and_about", "cooking", "gym", "getting_ready"],
        "moments": {
            "morning_coffee": (
                "coffee on a farmhouse porch at sunrise, flannel shirt "
                "and jeans, fields stretching out behind",
                "sunrise coffee on the porch before chores"),
            "out_and_about": (
                "walking a fence line in golden light, boots and a worn "
                "jacket, horses grazing nearby",
                "checking the fence line, horses keeping me company"),
            "gym": (
                "hauling hay bales by the barn, strong and sunlit, work "
                "gloves on",
                "who needs a gym when there's hay to haul"),
            "cooking": (
                "cooking in a farmhouse kitchen, sleeves rolled up, "
                "garden vegetables on the counter",
                "supper from the garden tonight"),
            "bedtime": (
                "porch swing at dusk, wrapped in a blanket, last light "
                "over the fields",
                "porch swing, day's done, sky doing its thing"),
        },
        "fallback": "honest work, good tired",
    },
    {
        "key": "coastal",
        "keywords": ("surf", "beach", "ocean", "sea", "sailor", "coast",
                     "boardwalk", "lifeguard", "swimmer", "island",
                     "harbor", "waves"),
        "pool": ["out_and_about", "gym", "cooking", "getting_ready"],
        "moments": {
            "morning_coffee": (
                "coffee on a boardwalk at sunrise, hoodie and salty "
                "hair, waves behind",
                "sunrise coffee, waves already calling"),
            "out_and_about": (
                "walking the boardwalk in afternoon sun, light summer "
                "outfit, the ocean right there",
                "boardwalk hours, nowhere to be but here"),
            "gym": (
                "morning run along the shoreline, athletic wear, sand "
                "and surf in the background",
                "shoreline miles before the crowds"),
            "bedtime": (
                "sitting on the sand at dusk watching the light go, "
                "cozy hoodie",
                "stayed for the whole sunset, worth it"),
        },
        "fallback": "salt air fixes most things",
    },
    {
        "key": "creative",
        "keywords": ("artist", "painter", "paint", "musician", "singer",
                     "songwriter", "writer", "author", "photographer",
                     "gallery", "studio", "poet", "dancer", "designer"),
        "pool": ["getting_ready", "out_and_about", "cooking", "gym"],
        "moments": {
            "morning_coffee": (
                "coffee in a sunlit studio apartment, sketches and "
                "notebooks spread around",
                "coffee first, then the work takes over"),
            "out_and_about": (
                "wandering a gallery district with a sketchbook, "
                "stylish casual outfit, street art behind",
                "out collecting things to steal for the work"),
            "getting_ready": (
                "trying on outfits in a cluttered, art-covered room, "
                "mirror selfie",
                "studio mess, outfit check"),
            "bedtime": (
                "curled up with a notebook in warm lamplight, ideas "
                "still coming",
                "one more idea before sleep, always one more"),
        },
        "fallback": "made something today, that's enough",
    },
    {
        "key": "fitness",
        "keywords": ("trainer", "athlete", "fitness", "yoga", "runner",
                     "running", "workout", "coach", "boxer", "gym",
                     "bodybuilder", "martial"),
        "pool": ["gym", "out_and_about", "cooking", "getting_ready"],
        "moments": {
            "morning_coffee": (
                "pre-workout coffee in bright athletic wear, gym bag "
                "by the door",
                "coffee, then we earn the day"),
            "out_and_about": (
                "cooling down after a run in the park, athletic wear, "
                "earbuds in",
                "park miles, head clear"),
            "cooking": (
                "meal-prepping in a bright kitchen, protein and greens "
                "lined up on the counter",
                "fuel first, everything else after"),
        },
        "fallback": "strong day, stronger tomorrow",
    },
]


def _profile_palette(character: dict[str, Any]) -> dict[str, Any] | None:
    """Pick the profile palette for a user-created character by keyword
    scoring over persona + blurb. None = the everyday default (stock
    moments, no overrides). Deterministic; ties go to the earlier
    palette in PROFILE_PALETTES."""
    text = ((character.get("persona") or "") + " "
            + (character.get("blurb") or "")).lower()
    best: dict[str, Any] | None = None
    best_score = 0
    for palette in PROFILE_PALETTES:
        score = sum(1 for kw in palette["keywords"] if kw in text)
        if score > best_score:
            best, best_score = palette, score
    return best


def _personal_moment_def(palette: dict[str, Any] | None, moment_key: str):
    """(post_type, sfw_scene, caption_desc) for a personal-set moment:
    palette override when the palette has one for this key, else the
    stock moment definition."""
    post_type, scene, desc = _moment_def(moment_key)
    if palette:
        override = (palette.get("moments") or {}).get(moment_key)
        if override:
            scene, desc = override
    return (post_type, scene, desc)


def _plan_personal_day(character: dict[str, Any], date_str: str,
                       palette: dict[str, Any] | None) -> list[tuple]:
    """Deterministic solo day plan for a user-created character.

    Same tuple shape as _plan_day — (moment_key, is_group, partner_ids,
    partner_names) — but always solo: a personal character's day never
    pulls roster girls into an owner-only set (and never vice versa).
    Seeded by character id + date, so it is reproducible per day.
    """
    seed = int(hashlib.md5(
        f"{character['id']}|{date_str}|herday-personal-v1".encode()
    ).hexdigest()[:8], 16)
    rng = random.Random(seed)
    pool = (palette or {}).get("pool") or ["gym", "cooking",
                                           "out_and_about", "getting_ready"]
    plan: list[tuple] = [("morning_coffee", False, [], [])]
    for _ in range(rng.randint(1, 3)):
        plan.append((rng.choice(pool), False, [], []))
    plan.append(("bedtime", False, [], []))
    if rng.random() < 0.7:
        plan.insert(-1, ("voice_rant", False, [], []))
    if rng.random() < 0.4:
        plan.insert(-1, (rng.choice(["shower_thought", "craving"]),
                         False, [], []))
    return plan


def _generate_personal_caption(char_name: str, persona: str, desc: str,
                               palette: dict[str, Any] | None = None,
                               cache_key: tuple | None = None) -> str:
    """Caption in the character's own voice, from their persona text —
    never the roster's girlfriend template (a user's character speaks
    as whoever they were written to be)."""
    if cache_key and cache_key in _caption_cache:
        return _caption_cache[cache_key]
    excerpt = re.sub(r"\s+", " ", (persona or "")).strip()[:500]
    who = f"You are {char_name}."
    if excerpt:
        who += f" This is who you are: {excerpt}"
    prompt = (
        f"{who} Write a short Instagram-story caption (1-2 sentences) "
        f"in your own voice for this moment: {desc}. Casual and real, "
        f"no hashtags, no emoji spam (max 1)."
    )
    text = _caption_via_proxy(prompt)
    if not text:
        text = (palette or {}).get("fallback") or "a little moment from today"
    if cache_key:
        _caption_cache[cache_key] = text
    return text


def _get_personal_characters() -> list[dict[str, Any]]:
    """Enabled user-created characters: companion-local rows carrying an
    owner_user_id. House roster rows (owner NULL) never appear here.
    Returns [{id, name, avatar_url, persona, blurb, owner_user_id}]
    sorted by owner then roster order, for deterministic caps."""
    try:
        from . import characters
        rows = characters._local_characters()
    except Exception:  # noqa: BLE001 — feed generation must survive this
        return []
    out: list[dict[str, Any]] = []
    for c in rows:
        owner = (c.get("owner_user_id") or "").strip()
        if not owner:
            continue
        out.append({
            "id": c["id"],
            "name": c.get("name") or c["id"],
            "avatar_url": c.get("avatar_url") or "",
            "persona": c.get("persona") or "",
            "blurb": c.get("blurb") or "",
            "owner_user_id": owner,
            "sort_order": c.get("sort_order") or 0,
        })
    out.sort(key=lambda c: (c["owner_user_id"], c["sort_order"], c["id"]))
    return out


_PERSONAL_SKIP_MARKERS = ("probe", "test")


def _eligible_owner(conn, owner_id: str) -> bool:
    """Whether personal sets may be generated for this owner: the
    account must exist and must not look like a probe/test account
    (owners are derived from character ownership, never by iterating
    accounts, so ordinary probes are excluded by construction)."""
    oid = (owner_id or "").strip()
    if not oid:
        return False
    if any(m in oid.lower() for m in _PERSONAL_SKIP_MARKERS):
        return False
    row = conn.execute(
        "SELECT email FROM users WHERE user_id = ?", (oid,)).fetchone()
    if row is None:
        return False
    local = (row[0] or "").split("@")[0].lower()
    if local.startswith(_PERSONAL_SKIP_MARKERS) or "probe" in local:
        return False
    return True


def personal_char_cap() -> int:
    """Max user-created characters per owner per day (env
    HER_DAY_PERSONAL_CHAR_CAP, default 3)."""
    try:
        return max(1, int(os.environ.get("HER_DAY_PERSONAL_CHAR_CAP", "3")))
    except ValueError:
        return 3


def personal_run_cap() -> int:
    """Max personal day-sets across one whole run (env
    HER_DAY_PERSONAL_MAX_SETS_PER_RUN, default 25) — a hard bound on
    the credits-billed image generations one run can add."""
    try:
        return max(1, int(os.environ.get(
            "HER_DAY_PERSONAL_MAX_SETS_PER_RUN", "25")))
    except ValueError:
        return 25


def generate_for_user_girl(user_id: str, girl_id: str,
                           girl_name: str = "Chloe",
                           plan: list[tuple] | None = None,
                           girl_map: dict[str, dict] | None = None,
                           date_str: str | None = None,
                           personal: dict[str, Any] | None = None
                           ) -> list[dict[str, Any]]:
    """Generate today's Whispr Feed posts for one target+girl pair.

    The target is normally the shared sentinel (store.HER_DAY_SHARED_USER):
    the posts are written once and every user reads them. Per-user targets
    remain supported for compatibility but are no longer written by the
    daily or on-demand flows.

    plan: [(moment_key, is_group, partner_ids, partner_names)] from
    _build_all_plans. If None, builds a solo-only plan for this girl.

    personal: for a user-created character's owner-scoped set, pass
    {"persona": ..., "palette": ...} — moment scenes/captions then come
    from the character's profile palette and own voice instead of the
    roster's stock moments and girlfriend caption template.
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
            if personal is not None:
                post_type, sfw_scene, desc_tpl = _personal_moment_def(
                    personal.get("palette"), moment_key)
            else:
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
            if personal is not None:
                caption = _generate_personal_caption(
                    girl_name, personal.get("persona") or "", desc,
                    personal.get("palette"),
                    ("personal",) + cache_key)
            else:
                caption = _generate_caption(girl_name, moment_key, desc,
                                            pnames, cache_key)
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


def generate_shared_day(date_str: str | None = None) -> dict[str, Any]:
    """Build today's shared Her Day set: one generation per girl, stored
    under the shared sentinel for every user to read.

    Idempotent per girl: a girl whose shared set already exists today is
    skipped. Per-girl failures are collected in `errors`, not fatal.
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    all_girls = _get_all_girls()
    plans = _build_all_plans(all_girls, date_str)
    girl_map = {g["id"]: g for g in all_girls}
    report: dict[str, Any] = {"generated": 0, "skipped": 0, "errors": [],
                              "girls": len(all_girls), "shared": True}
    for g in all_girls:
        try:
            posts = generate_for_user_girl(
                store.HER_DAY_SHARED_USER, g["id"], g["name"],
                plan=plans.get(g["id"], []),
                girl_map=girl_map,
                date_str=date_str,
            )
            if posts:
                report["generated"] += len(posts)
            else:
                report["skipped"] += 1
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"{g['id']}: {e}")
    return report


def generate_personal_days(date_str: str | None = None,
                           only_owner: str | None = None) -> dict[str, Any]:
    """Generate today's day-set for every eligible user-created character.

    One set per character per day, stored under the owner's user_id —
    only that owner's feed ever reads it back. Owners are derived from
    character ownership (never by iterating accounts); probe/test
    accounts are skipped. Bounded by personal_char_cap() per owner and
    personal_run_cap() per run. Idempotent per character: a character
    whose set already exists today is skipped.
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    report: dict[str, Any] = {"generated": 0, "sets": 0, "skipped": 0,
                              "errors": [], "owners": 0}
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for c in _get_personal_characters():
        by_owner.setdefault(c["owner_user_id"], []).append(c)
    if not by_owner:
        return report

    conn = db.connect()
    try:
        eligible = {oid: _eligible_owner(conn, oid) for oid in by_owner}
    finally:
        conn.close()

    cap = personal_char_cap()
    run_cap = personal_run_cap()
    for oid in sorted(by_owner):
        if only_owner and oid != only_owner:
            continue
        owned = by_owner[oid]
        if not eligible.get(oid):
            report["skipped"] += len(owned)
            continue
        report["owners"] += 1
        for char in owned[:cap]:
            if report["sets"] >= run_cap:
                report["skipped"] += 1
                continue
            palette = _profile_palette(char)
            try:
                posts = generate_for_user_girl(
                    oid, char["id"], char["name"],
                    plan=_plan_personal_day(char, date_str, palette),
                    girl_map={char["id"]: char},
                    date_str=date_str,
                    personal={"persona": char.get("persona") or "",
                              "palette": palette},
                )
            except Exception as e:  # noqa: BLE001 — one char never sinks the run
                report["errors"].append(f"{char['id']}: {e}")
                continue
            if posts:
                report["generated"] += len(posts)
                report["sets"] += 1
            else:
                report["skipped"] += 1
    return report


def generate_for_all_girls(user_id: str) -> int:
    """On-demand Whispr Feed generation (POST /api/v1/her-day/generate).

    The roster day-set is shared now: this ensures today's shared set
    exists, plus today's day-sets for the CALLER's own user-created
    characters. Returns how many posts THIS call created (0 when the
    sets were already there). Other users' characters are never touched
    by someone else's generate call.
    """
    created = generate_shared_day()["generated"]
    created += generate_personal_days(only_owner=user_id)["generated"]
    return created


def generate_daily() -> dict[str, Any]:
    """Generate today's Whispr Feed sets. Called by the 6 AM cron.

    The shared roster pass runs first (one generation total; user
    accounts are never iterated, so probe/test accounts get nothing
    generated or written for them), then owner-scoped personal sets
    for user-created characters. The top-level "generated"/"skipped"/
    "errors" keys remain the shared-pass numbers, exactly as before;
    personal counts ride along under personal_* keys.
    """
    report = generate_shared_day()
    personal = generate_personal_days()
    return {"generated": report["generated"],
            "skipped": report["skipped"],
            "errors": report["errors"],
            "personal_generated": personal["generated"],
            "personal_sets": personal["sets"],
            "personal_skipped": personal["skipped"],
            "personal_owners": personal["owners"],
            "personal_errors": personal["errors"]}
