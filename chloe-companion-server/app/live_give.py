"""Live give creation — a fresh give scene made in the moment.

Until now a give moment in chat only ever served a pre-made preset from
her library (POST /api/v1/preset-video). For the four private girls
(brooklyn, hannah, jordan, mia), in OWNER chats only, she can now CREATE
a brand-new give scene during the conversation itself:

- He explicitly asks for something new ("make me one", "send me
  something new", "make me a fresh video", "show me you ..."), or
  plainly asks for a give of her with an explicit action ("make me
  a video of you taking off your bra") — the routing fix of
  2026-10-14: those asks are claimed by this hook BEFORE the
  assistant lane can take the turn, or
- on her own initiative when his message carries the chat path's
  existing explicit-heat action signal (the same detector family
  main._maybe_save_requested_action uses) — initiative renders a
  picture (fast), never a video.

There is deliberately no new "heat engine": the codebase tracks no
in-turn give-moment signal (the /give flow keys on the client calling
the preset endpoint), so initiative gates on that existing explicit
detector plus a long cooldown, and nothing else.

Flow: the chat hook (main.chat, after maybe_assist, before the reply is
composed) calls maybe_start_live_give(). On a trigger it starts a
background render thread and returns a brief; main copies the brief
into context["assist_result"], so her normal reply — generated exactly
as usual, never blocked on the render — confirms in her own voice that
she's making it. The worker renders through assist's plan-session seam
(the _locked_render gate: _sogni_plan_run / _sogni_plan_image_run, the
same funnel give_gen and her_day use), face-locked to HER OWN avatar /
reference frames only — never a user-uploaded photo — and lands the
finished media in the same chat via store.log_turn with a media
attachment, the shape _sogni_plan_deliver uses. On ANY failure (no
session, dead session after the shared resolve+retry, render error)
the worker falls back to the preset from her own give library
closest to what he asked for — the ask's stage is derived from his
words (stage_for_ask_text), with the progression walk as the
no-stage-named fallback; with no preset either, nothing is surfaced
and the conversation simply continues.

Session discipline is inherited, not reinvented: the stored plan pair,
assist.resolve_dead_sogni_session for recovery, and give_gen's
single-flight hardwired sign-in for these owner-scoped girls. Nothing
here ever clears a session or starts a raw login.

Dedup/cooldown: at most one live-create in flight per (user, girl) —
a second trigger while one renders serves a preset immediately (once
per flight) instead of queueing a second render. Any start also
blocks the next start for COOLDOWN_S; initiative starts are blocked
for INITIATIVE_COOLDOWN_S.
"""
from __future__ import annotations

import os
import re
import threading
import time
import urllib.parse
from typing import Any

from . import assist, characters, config, db, give_gen, store

# The four private girls. Owner-scoped companion-local characters;
# live creation never applies to anyone else, however the chat reads.
LIVE_GIVE_GIRLS = frozenset({"brooklyn", "hannah", "jordan", "mia"})

COOLDOWN_S = 120.0            # any start blocks the next start, per chat
INITIATIVE_COOLDOWN_S = 600.0  # initiative starts are rarer by design

# ---------------------------------------------------------------------------
# Trigger matchers (tight on purpose: a false positive burns a render,
# a miss just serves the normal chat — misses are cheaper).
# ---------------------------------------------------------------------------

_FRESH_RE = re.compile(
    r"\b(new|fresh|another|brand[-\s]new|just\s+for\s+me)\b", re.IGNORECASE)
_CREATE_VERB_RE = re.compile(
    r"\b(make|create|send|give|show|record|film|take|shoot)\b", re.IGNORECASE)
_MEDIA_NOUN_RE = re.compile(
    r"\b(video|clip|movie|pic|pics|picture|pictures|photo|photos|image|"
    r"selfie|one)\b", re.IGNORECASE)
_VIDEO_WORD_RE = re.compile(
    r"\b(video|videos|clip|clips|movie|moving)\b", re.IGNORECASE)
_IMAGE_WORD_RE = re.compile(
    r"\b(pic|pics|picture|pictures|photo|photos|image|images|selfie)\b",
    re.IGNORECASE)
_SOMETHING_NEW_RE = re.compile(r"\bsomething\s+new\b", re.IGNORECASE)
_MAKE_ONE_RE = re.compile(
    r"\b(?:make|send|give|create)\s+(?:me\s+)?one\b|\banother\s+one\b",
    re.IGNORECASE)
_SHOW_ME_YOU_RE = re.compile(r"\bshow\s+me\s+you\b", re.IGNORECASE)

# Mirrors main._get_action_re — the chat path's existing explicit-action
# detector (it saves these asks as give presets). Kept in step
# deliberately: main imports this module, so the pattern is mirrored
# here rather than imported. Gerund forms included (2026-10-14):
# "taking off your bra" is how the ask actually arrives mid-sentence;
# the bare imperatives alone missed it.
_ACTION_ASK_RE = re.compile(
    r"\b(show me|let me see|wanna see|want to see|sucking|suck|"
    r"licking|lick|touching|touch|showing|show|stripping|strip|"
    r"taking off|take off|removing|remove)\b.{0,40}\b"
    r"(tits|titties|boobs|breasts|pussy|ass|dick|cock|nipples|body|"
    r"thighs|panties|bra)\b",
    re.IGNORECASE,
)

# "... of you ..." — the ask is about HER (not a barn, not the site).
_OF_YOU_RE = re.compile(r"\bof\s+(?:you|yourself)\b", re.IGNORECASE)

# Chat-context scene flavor: a fixed vocabulary of settings, scanned
# over the last few turns and rephrased — his words themselves never
# enter the render prompt.
_SETTING_RES: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(pat, re.IGNORECASE), phrase)
    for pat, phrase in (
        (r"\bshower\b", "in the shower"),
        (r"\bbathroom\b", "in the bathroom"),
        (r"\bon the bed\b|\bbedroom\b", "in the bedroom"),
        (r"\bcouch\b|\bsofa\b", "on the couch"),
        (r"\bkitchen\b", "in the kitchen"),
        (r"\bporch\b", "on the porch"),
        (r"\bpool\b", "by the pool"),
        (r"\bbeach\b", "at the beach"),
        (r"\boffice\b", "in the office"),
        (r"\bin the car\b|\bcar\b", "in the car"),
        (r"\bwindow\b", "by the window"),
        (r"\bgarden\b", "in the garden"),
        (r"\bhotel\b", "in a hotel room"),
        (r"\bliving\s+room\b", "in the living room"),
    )
)

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _media_kind_for(text: str, default: str) -> str:
    """Explicit media words pick the kind; the earlier word wins when
    both appear. No media word -> the caller's default."""
    mv = _VIDEO_WORD_RE.search(text or "")
    mi = _IMAGE_WORD_RE.search(text or "")
    if mv and mi:
        return "video" if mv.start() <= mi.start() else "image"
    if mv:
        return "video"
    if mi:
        return "image"
    return default


def detect_live_give_ask(text: str) -> dict[str, Any] | None:
    """An explicit ask for a NEW give scene, or None.

    Tight by design: a creation verb plus novelty/freshness language,
    a direct "make me one", "show me you ..." backed by fresh or
    explicit-heat wording — or (2026-10-14 routing fix) a plain
    "make me a video/pic of you ..." carrying an explicit action or
    adult signal: that is exactly how the owner asks a private girl
    for a give, and it used to fall through to the assistant lane,
    which ate it silently. A plain scene ask with no heat ("make me
    a video of you dancing") still belongs to the assistant lane and
    never matches here.
    """
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t or assist._VIDEO_CALL_RE.search(t):
        return None
    hit = False
    if _SOMETHING_NEW_RE.search(t) and _CREATE_VERB_RE.search(t):
        hit = True
    elif _MAKE_ONE_RE.search(t):
        hit = True
    elif _FRESH_RE.search(t) and _CREATE_VERB_RE.search(t) \
            and _MEDIA_NOUN_RE.search(t):
        hit = True
    elif _CREATE_VERB_RE.search(t) and _MEDIA_NOUN_RE.search(t) \
            and _OF_YOU_RE.search(t) and (
                _ACTION_ASK_RE.search(t)
                or assist._STRONG_ADULT_MEDIA_RE.search(t)):
        hit = True
    elif _SHOW_ME_YOU_RE.search(t) and (
            _FRESH_RE.search(t) or _ACTION_ASK_RE.search(t)
            or assist._STRONG_ADULT_MEDIA_RE.search(t)):
        hit = True
    if not hit:
        return None
    return {"kind": _media_kind_for(t, "video"), "initiative": False}


def detect_initiative_ask(text: str) -> dict[str, Any] | None:
    """Her initiative, gated on the codebase's existing explicit-heat
    action signal only — there is no in-turn give-moment classifier to
    reuse, and none is invented here. Always a picture (fast)."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t or assist._VIDEO_CALL_RE.search(t):
        return None
    if detect_live_give_ask(t):
        return None  # the explicit path owns this message
    if _ACTION_ASK_RE.search(t):
        return {"kind": "image", "initiative": True}
    return None


# ---------------------------------------------------------------------------
# Ask text -> give stage. The fallback picker serves the preset closest
# to WHAT WAS ASKED (2026-10-14 picker fix), so the ask's stage is
# derived from its text against give_gen's stage vocabulary (STAGE_
# NAMES / STAGE_DESCRIPTIONS, both genders — one shared table, keyed to
# the acts those stages name). This is the ONE mapping: the live hook
# derives it from his chat message and custom_video derives it from the
# request description. Tight by design, like the detectors: a text that
# names no stage act returns None and the picker keeps its legacy
# progression walk, unchanged.
# ---------------------------------------------------------------------------

_STAGE_ASK_RES: tuple[tuple[int, re.Pattern], ...] = tuple(
    (stage, re.compile(pat, re.IGNORECASE))
    for stage, pat in (
        # 7 "titty fuck" / between-her-breasts: its "fuck" is not
        # stage 9's — stage 9's fuck pattern below requires an object.
        (7, r"\btit(?:ty)?\s*fuck\b|\bdeep\s+thrusts?\b"
            r"|\bbetween\b[^.!?]{0,20}\b(boobs|tits|breasts|titties)\b"),
        (9, r"\bsex\b|\bmake\s+love\b|\bpenetrat\w*\b"
            r"|\binside\s+(you|me|her)\b"
            r"|(?<!titty )(?<!tit )\bfuck\b[^.!?]{0,20}\b(me|her|him|you)\b"
            r"|\bride\b[^.!?]{0,12}\b(me|my\s+cock|my\s+dick)\b"),
        (8, r"\boral\b|\bblow\s*job\b|\bblowjob\b"
            r"|\bgiv\w*\b[^.!?]{0,12}\bhead\b|\bgetting\s+head\b|\bbj\b"
            r"|\bsuck\b[^.!?]{0,20}\b(cock|dick|me|it|him)\b"
            r"|\blick\b[^.!?]{0,16}\b(cock|dick)\b"),
        (6, r"\bmasturbat\w*\b|\bplays?\s+w/\s*(herself|yourself|himself)\b"
            r"|\b(play|plays|playing|touch|touching|rub|rubbing|finger|"
            r"fingering|stroke|stroking)\b[^.!?]{0,20}"
            r"\b(yourself|herself|himself)\b"),
        (5, r"\b(pants|jeans|shorts)\s+off\b"
            r"|\b(take|taking|pull|remove|removing)\s+off\b[^.!?]{0,16}"
            r"\b(pants|jeans|shorts|panties)\b"
            r"|\b(take|taking|pull|remove|removing)\b[^.!?]{0,20}"
            r"\b(pants|jeans|shorts)\b[^.!?]{0,8}\boff\b"
            r"|\bunderwear\b|\bpanties\b"),
        # 4's signature is the nipples themselves (pinch/tease/touch).
        (4, r"\bnipples?\b"),
        (3, r"\b(grab|grabbing|touch|touching|hold|holding|squeeze|"
            r"squeezing|cup|feel|feeling|rub|rubbing)\b[^.!?]{0,20}"
            r"\b(boobs|breasts|tits|titties|chest)\b"
            r"|\b(show|see)\b[^.!?]{0,16}\b(boobs|breasts|tits|titties)\b"
            r"|\bboob\s*grabs?\b|\bchest\s+flex\b"
            r"|\bflex\b[^.!?]{0,16}\b(chest|arms)\b"),
        (2, r"\btopless\b|\bshirtless\b|\b(top|shirt|bra)\s+off\b"
            r"|\b(take|taking|pull|slip|peel|remove|removing)\s+off\b"
            r"[^.!?]{0,16}\b(top|shirt|bra)\b"
            r"|\b(take|taking|pull|slip|peel|remove|removing)\b"
            r"[^.!?]{0,20}\b(top|shirt|bra)\b[^.!?]{0,8}\boff\b"),
        (1, r"\b(lift|lifting)\b[^.!?]{0,16}\b(shirt|top)\b"
            r"|\bshirt\s+lift\b|\blift\s+tease\b"),
    )
)

# A bare "fuck" with no more specific stage evidence still reads as
# stage 9 — but only as the last resort, so "titty fuck" (stage 7's
# own pattern above) is never escalated by it.
_BARE_STAGE_NINE_RE = re.compile(r"\bfuck(?:ing)?\b", re.IGNORECASE)


def stage_for_ask_text(text: str) -> int | None:
    """The give stage an ask's text names, or None when it names none.

    Every stage whose act the text mentions is a candidate; the ask's
    peak (the highest) wins — an ask that escalates ("take off your
    top and play with yourself") is a request for its furthest point,
    and the fallback should match that heat, not its first clause.
    Shared by the live hook (his message) and custom_video (the
    request description) so both fallbacks read an ask the same way."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return None
    hits = {stage for stage, pat in _STAGE_ASK_RES if pat.search(t)}
    if not hits and _BARE_STAGE_NINE_RE.search(t):
        hits = {9}
    return max(hits) if hits else None


# ---------------------------------------------------------------------------
# Gives follow the conversation (improvement #3, 2026-10-19): a give
# lands mid-conversation, so its stage and her line read the
# conversation it lands in — the active roleplay scene (brain's
# documented read interface, brain.get_scene_state) and his recent
# turns — instead of the progression counter and a generic label
# alone. The stage read runs through the ONE ask->stage mapping
# above; scene/caption reads never raise and never gate a give.
# ---------------------------------------------------------------------------

def _his_recent_texts(conn: Any, user_id: str, girl_id: str,
                      limit: int = 6) -> list[str]:
    """His recent turn texts, oldest first. The stage vocabulary is
    read over HIS words (plus the scene brief, which is his casting)
    — never over hers, so her own lines can't escalate a pick."""
    try:
        return [(t.get("text") or "")
                for t in store.recent_turns(conn, user_id, limit, girl_id)
                if t.get("role") == "user"]
    except Exception:  # noqa: BLE001 — a context read must never break a give
        return []


def _active_scene_brief(conn: Any, user_id: str, girl_id: str) -> str:
    """The active scene's brief (his casting, verbatim), or "" when
    no scene is active. brain is imported lazily: it documents
    get_scene_state as the read interface for other features, and a
    lazy import keeps this module's import graph unchanged."""
    try:
        from . import brain
        scene = brain.get_scene_state(conn, user_id, girl_id)
    except Exception:  # noqa: BLE001
        return ""
    if scene and scene.get("state") == "active":
        return (scene.get("brief") or "").strip()
    return ""


def scene_stage_hint(conn: Any, user_id: str, girl_id: str,
                     extra_text: str = "") -> int | None:
    """The stage the current conversation is at, or None when it
    names none: the active scene brief and his recent turns read
    through stage_for_ask_text. Callers clamp to
    store.give_stage_cap — this hint never widens anyone's ceiling,
    so a heated scene in a group/event chat still serves <= stage 3."""
    texts = []
    brief = _active_scene_brief(conn, user_id, girl_id)
    if brief:
        texts.append(brief)
    if extra_text:
        texts.append(extra_text)
    texts.extend(_his_recent_texts(conn, user_id, girl_id))
    if not texts:
        return None
    return stage_for_ask_text(" \n".join(texts))


def _is_event_account(user_id: str | None) -> bool:
    """Synthetic Telegram group/event account (the shared surface
    behind store.give_stage_cap's stage-3 rule)."""
    uid = (user_id or "").strip()
    return uid.startswith("tg_") and store.give_stage_cap(uid) == 3


def give_caption(conn: Any, user_id: str, girl_id: str, *,
                 kind: str = "video",
                 fallback: str | None = None) -> str | None:
    """One short in-character line to land WITH a give, referencing
    what they were just talking about: the active scene when there
    is one, else the recent turns riding in the generation context.
    Generated through brain.gemini_generate (the chat model seam)
    and speech-policed like every other generated line. ANY failure
    — synthetic event account (shared surface; its caption stays the
    plain label), no conversation to reference, model down, policed
    or overlong line — returns `fallback`, so every caller keeps its
    existing line verbatim. Never raises."""
    try:
        if not user_id or _is_event_account(user_id):
            return fallback
        brief = _active_scene_brief(conn, user_id, girl_id)
        try:
            turns = store.recent_turns(conn, user_id, 6, girl_id)
        except Exception:  # noqa: BLE001
            turns = []
        if not brief and not turns:
            return fallback
        from . import brain
        character = characters.get_character(girl_id, user_id=user_id) or {}
        name = character.get("name") or girl_id
        context = {
            "character": character,
            "character_name": name,
            "recent": [{"role": t.get("role"),
                        "text": t.get("text") or ""} for t in turns],
        }
        noun = "picture" if kind == "image" else "video"
        if brief:
            ask = (f"You two are in the middle of a scene he set: {brief}"
                   f"\n\nWrite ONE short line in your voice to send him "
                   f"with this {noun} of you — it continues that scene, "
                   f"so reference what you two were just doing and "
                   f"talking about. Pick the moment up, don't restart "
                   f"it. Warm and wanting, one line, no quotation "
                   f"marks, no labels.")
        else:
            ask = (f"Write ONE short line in your voice to send him "
                   f"with this {noun} of you. Reference what you two "
                   f"were just talking about in the messages above — "
                   f"pick the moment up, don't restart it or change "
                   f"the subject. Warm and wanting, one line, no "
                   f"quotation marks, no labels.")
        # Key Manager v2: give captions are batch work at the pool.
        # The lane rides the context dict (see brain.gemini_generate)
        # so the established 3-argument generate seam stays intact.
        if isinstance(context, dict):
            context["pool_lane"] = "batch"
        line = brain.gemini_generate(ask, context, girl_id)
        if line:
            line = " ".join(line.strip().strip('"').split())
            if line and len(line) <= 220 \
                    and not brain._polices_his_speech(line):
                print(f"[give_follow] caption girl={girl_id} "
                      f"user={user_id}", flush=True)
                return line
    except Exception as e:  # noqa: BLE001 — a caption must never break a give
        print(f"[give_follow] caption failed for {girl_id}: {e}",
              flush=True)
    return fallback


# ---------------------------------------------------------------------------
# Per-(user, girl) state: in-flight flag + cooldown clocks. In-process,
# like main's _LAST_PRESET_SENT — a chat-speed feature, not a queue.
# ---------------------------------------------------------------------------

_STATE_LOCK = threading.Lock()
_STATE: dict[tuple[str, str], dict[str, Any]] = {}


def _state_for(key: tuple[str, str]) -> dict[str, Any]:
    return _STATE.setdefault(key, {
        "in_flight": False,
        "last_start": 0.0,
        "last_initiative": 0.0,
        "preset_sent_in_flight": False,
    })


def _reset_state_for_tests() -> None:
    with _STATE_LOCK:
        _STATE.clear()


def _next_stage(conn: Any, user_id: str, girl_id: str) -> int:
    """Her next give-escalation stage for this user — the same
    progression the preset endpoint serves: (gives % 9) + 1. When
    the conversation is somewhere (scene_stage_hint), the render
    continues THAT stage instead — clamped to the same cap."""
    cap = store.give_stage_cap(user_id)
    hint = scene_stage_hint(conn, user_id, girl_id)
    if hint is not None:
        return min(int(hint), cap)
    try:
        row = conn.execute(
            "SELECT gives_received FROM user_give_progress "
            "WHERE user_id = ? AND girl_id = ?",
            (user_id, girl_id)).fetchone()
        gives = int(row[0]) if row else 0
    except Exception:  # noqa: BLE001 — progression is a nicety here
        gives = 0
    return (gives % cap) + 1


# ---------------------------------------------------------------------------
# Preset fallback — her OWN give library, mirroring preset_video's
# stage walk (DB rows first, then the static media-dir library).
#
# Which stage serves (2026-10-14 picker fix): when the ask's stage is
# known (stage_for_ask_text over his message / the request
# description), the pick is ASK-aware — her preset at that stage, else
# the nearest stage by distance, ties preferring the stage at-or-below
# the ask before the one above. Production proved the old behaviour
# wrong: the picker consulted only his progression, so "take off your
# top" was answered with a stage-8 clip because his walk sat at 8.
# With no stage derivable, the legacy progression walk below runs
# exactly as it always has.
# ---------------------------------------------------------------------------

def _media_type_of(url: str) -> str:
    u = (url or "").split("?", 1)[0].lower()
    return "image" if u.endswith(_IMAGE_EXTS) else "video"


def _static_media_dir() -> str:
    """The shipped static give library (<girl>-give-stage<N>.mp4),
    served under /media/."""
    return os.path.join(os.path.dirname(config.DATA_DIR),
                        "chloe-companion-app", "media")


def _stage_order_from(want_stage: int, cap: int = 9) -> list[int]:
    """Stage visit order for an ask-aware pick: the asked stage first,
    then by distance, the stage below before the stage above at each
    distance (1 away below, 1 away above, 2 below, 2 above, ...).
    Never leaves 1..cap — group/event accounts cap at 3."""
    return sorted(range(1, cap + 1),
                  key=lambda s: (abs(s - want_stage),
                                 0 if s <= want_stage else 1))


def _prefer_within_stage(rows: list[dict[str, Any]], want_kind: str,
                         seen: set) -> dict[str, Any] | None:
    """The picker's standing within-stage preferences, unchanged:
    the requested kind first, then unseen clips, then row order
    (id ASC for DB rows, filename order for the static library)."""
    if not rows:
        return None
    kind_rows = [r for r in rows if r["media_type"] == want_kind] or rows
    fresh = [r for r in kind_rows if r["id"] not in seen] or kind_rows
    return fresh[0]


def _pick_preset(conn: Any, user_id: str, girl_id: str,
                 want_kind: str,
                 want_stage: int | None = None) -> dict[str, Any] | None:
    """Closest preset from her own library. With `want_stage` (the
    ask's stage, stage_for_ask_text) the closest is to the ASK: exact
    stage, else nearest by distance, below before above. Without it,
    the legacy progression walk, byte-for-byte the old behaviour.
    Within a stage: the requested kind, then unseen, then row order."""
    if want_stage is not None and not (1 <= int(want_stage) <= 9):
        want_stage = None
    # Group/event accounts cap at stage 3 (owner rule, 2026-10-18):
    # the ask clamps, the progression wraps inside the cap, and the
    # ask-aware visit order never leaves it.
    cap = store.give_stage_cap(user_id)
    if want_stage is not None:
        want_stage = min(int(want_stage), cap)
    try:
        row = conn.execute(
            "SELECT gives_received FROM user_give_progress "
            "WHERE user_id = ? AND girl_id = ?",
            (user_id, girl_id)).fetchone()
        gives = int(row[0]) if row else 0
        seen = {r[0] for r in conn.execute(
            "SELECT media_id FROM user_give_seen "
            "WHERE user_id = ? AND girl_id = ?",
            (user_id, girl_id)).fetchall()}
    except Exception:  # noqa: BLE001
        gives, seen = 0, set()
    if want_stage is not None:
        return _pick_preset_for_stage(conn, girl_id, want_kind,
                                      int(want_stage), seen, cap=cap)
    stage = (gives % cap) + 1
    rows: list[dict[str, Any]] = []
    try:
        for try_stage in list(range(stage, cap + 1)) + list(range(1, stage)):
            found = conn.execute(
                "SELECT id, url FROM girl_media WHERE girl_id = ? "
                "AND kind = 'give' AND stage = ? ORDER BY id ASC",
                (girl_id, try_stage)).fetchall()
            if found:
                rows = [{"id": r[0], "url": r[1],
                         "media_type": _media_type_of(r[1]),
                         "stage": try_stage} for r in found]
                break
    except Exception:  # noqa: BLE001
        rows = []
    if not rows:
        # Static media-dir library (e.g. brooklyn's shipped clips):
        # <girl>-give-stage<N>[-variant].mp4, served under /media/.
        try:
            media_dir = _static_media_dir()
            for fn in sorted(os.listdir(media_dir)):
                fl = fn.lower()
                if not fl.startswith(girl_id + "-give-") \
                        or not fl.endswith(".mp4"):
                    continue
                m = re.search(r"-stage(\d+)", fl)
                st = int(m.group(1)) if m else 0
                if 1 <= st <= cap:
                    rows.append({"id": None, "url": "/media/" + fn,
                                 "media_type": "video", "stage": st})
        except OSError:
            pass
    if not rows:
        return None
    kind_rows = [r for r in rows if r["media_type"] == want_kind] or rows
    fresh = [r for r in kind_rows if r["id"] not in seen] or kind_rows
    return fresh[0]


def _pick_preset_for_stage(conn: Any, girl_id: str, want_kind: str,
                           want_stage: int,
                           seen: set, cap: int = 9) -> dict[str, Any] | None:
    """Ask-aware pick: her presets grouped by stage (DB rows first;
    the static library only when she has no staged DB rows at all —
    the same precedence as the legacy walk), visited in
    _stage_order_from order."""
    by_stage: dict[int, list[dict[str, Any]]] = {}
    try:
        found = conn.execute(
            "SELECT id, url, stage FROM girl_media WHERE girl_id = ? "
            "AND kind = 'give' ORDER BY id ASC",
            (girl_id,)).fetchall()
        for r in found:
            st = int(r[2] or 0)
            if 1 <= st <= 9:
                by_stage.setdefault(st, []).append(
                    {"id": r[0], "url": r[1],
                     "media_type": _media_type_of(r[1]), "stage": st})
    except Exception:  # noqa: BLE001
        by_stage = {}
    if not by_stage:
        try:
            media_dir = _static_media_dir()
            for fn in sorted(os.listdir(media_dir)):
                fl = fn.lower()
                if not fl.startswith(girl_id + "-give-") \
                        or not fl.endswith(".mp4"):
                    continue
                m = re.search(r"-stage(\d+)", fl)
                st = int(m.group(1)) if m else 0
                if 1 <= st <= 9:
                    by_stage.setdefault(st, []).append(
                        {"id": None, "url": "/media/" + fn,
                         "media_type": "video", "stage": st})
        except OSError:
            pass
    for st in _stage_order_from(want_stage, cap):
        pick = _prefer_within_stage(by_stage.get(st) or [], want_kind,
                                    seen)
        if pick:
            return pick
    return None


def _preset_size_bytes(preset: dict[str, Any]) -> int | None:
    """A preset's real byte size when its URL resolves to a local
    file — /generated/ rows (fresh renders registered in girl_media)
    and the static /media/ library, by bare path or full URL alike.
    girl_media carries no size column, so the filesystem is the one
    source. None when the size cannot be determined (remote URL only,
    file gone): the delivery then omits the size rather than recording
    a false 0 (the client renders a missing size as no label at all)."""
    raw = (preset.get("url") or "").split("?", 1)[0]
    if not raw:
        return None
    path = urllib.parse.urlparse(raw).path if "://" in raw else raw
    base = os.path.basename(path)
    if not base:
        return None
    candidates = []
    if path.startswith("/generated/"):
        candidates.append(os.path.join(config.GENERATED_DIR, base))
    elif path.startswith("/media/"):
        candidates.append(os.path.join(_static_media_dir(), base))
    for cand in candidates:
        try:
            size = os.path.getsize(cand)
        except OSError:
            continue
        if size > 0:
            return int(size)
    return None


def _deliver_preset(conn: Any, user_id: str, girl_id: str,
                    preset: dict[str, Any], *, during_render: bool,
                    line: str | None = None) -> None:
    """Land a library preset in the chat + the preset endpoint's
    progress bookkeeping (DB rows only). `line` overrides the spoken
    line (the custom-video fallback speaks its own swap line); the
    default lines are unchanged. The attachment records the file's
    real size when it can be determined (2026-10-14) — never a
    placeholder 0 for an existing file."""
    kind = preset["media_type"]
    ext = "mp4" if kind == "video" else \
        (preset["url"].split("?", 1)[0].rsplit(".", 1)[-1] or "jpg")
    attachment = {"kind": kind, "url": preset["url"],
                  "name": f"{girl_id}-give.{ext}"}
    size = _preset_size_bytes(preset)
    if size:
        attachment["size"] = size
    attachments = [attachment]
    if line is None:
        default = ("here, babe — hold onto this one while I make your "
                   "new one." if during_render else
                   "here, babe — one of mine, just for you.")
        # Improvement #3: her line references the conversation when
        # it can; the standing lines remain the fallback verbatim.
        line = give_caption(conn, user_id, girl_id, kind=kind,
                            fallback=default)
    store.log_turn(conn, user_id, girl_id, line, girl_id, attachments)
    if preset.get("id"):
        try:
            conn.execute(
                "INSERT INTO user_give_progress (user_id, girl_id,"
                " gives_received) VALUES (?, ?, 1)"
                " ON CONFLICT (user_id, girl_id) DO UPDATE SET"
                " gives_received = gives_received + 1,"
                " updated_at = datetime('now')",
                (user_id, girl_id))
            conn.execute(
                "INSERT OR IGNORE INTO user_give_seen"
                " (user_id, girl_id, media_id, stage) VALUES (?, ?, ?, ?)",
                (user_id, girl_id, preset["id"], preset.get("stage") or 0))
            conn.commit()
        except Exception:  # noqa: BLE001 — bookkeeping never breaks chat
            pass


# ---------------------------------------------------------------------------
# Scene construction: her stage scene for this moment + a rephrased
# setting drawn from the last few turns via a fixed vocabulary. His
# words are never copied into the prompt.
# ---------------------------------------------------------------------------

def _build_scene(conn: Any, user_id: str, girl_id: str, stage: int,
                 trigger_text: str) -> str:
    desc = give_gen.stage_description_for(girl_id, stage)
    texts = [trigger_text or ""]
    try:
        for turn in store.recent_turns(conn, user_id, 6, girl_id):
            texts.append(turn.get("text") or "")
    except Exception:  # noqa: BLE001
        pass
    blob = " \n".join(texts)
    scene = desc
    for pattern, phrase in _SETTING_RES:
        if pattern.search(blob) and phrase not in desc.lower():
            scene = f"{desc}, {phrase}"
            break
    return scene


# ---------------------------------------------------------------------------
# Render worker: plan-session seam inside the shared gate, resolve +
# one retry on a dead session, preset fallback on any failure.
# ---------------------------------------------------------------------------

def _run_seam(session: dict, kind: str, prompt: str,
              ref_path: str) -> tuple[dict, dict | None]:
    if kind == "image":
        return assist._sogni_plan_image_run(
            session, prompt, ref_path, config.SOGNI_IMAGE_MODEL)
    return assist._sogni_plan_run(
        session, prompt, ref_path,
        model=config.SOGNI_VIDEO_MODEL, duration=15)


def _render_fresh(conn: Any, girl: dict[str, Any], kind: str,
                  scene: str, stage: int) -> dict | None:
    """One fresh render of her, holding the live-render priority
    signal for the whole attempt (session fetch + render + heal):
    while a live request is in flight the give_gen queue pauses
    between jobs instead of competing for the session or consuming
    the single-flight sign-in first."""
    with assist.live_render_priority():
        return _render_fresh_inner(conn, girl, kind, scene, stage)


def _render_fresh_inner(conn: Any, girl: dict[str, Any], kind: str,
                        scene: str, stage: int) -> dict | None:
    """One fresh render of her, or None when no plan session exists.
    Mirrors give_gen's job: stored pair first, her hardwired
    single-flight sign-in when absent. A pair that dies mid-render is
    healed at the seam (assist._locked_render resolves + re-runs
    once); the resolve + one retry below remains only for an outcome
    the seam could not heal. An api_key-typed stored session is not a
    plan session (her_day's stance) and is never used here."""
    girl_id = girl["id"]
    session = assist.stored_sogni_session(conn)
    if session and session.get("api_key"):
        session = None
    if not session:
        session = give_gen._hardwired_sogni_session(
            conn, girl_id, caller="live_give")
    if not session:
        return None
    ref_path = None
    if stage > 2:
        ref_path = give_gen._topless_reference_frame(conn, girl_id)
    if not ref_path:
        # Her own avatar as the face-lock reference (media_gen's
        # fetcher, via assist's app-relative retry wrapper — the
        # same helper give_gen uses). Never a user upload.
        ref_path = assist._fetch_first_frame(girl.get("avatar_url") or "")
    if not ref_path:
        return None
    try:
        prompt = assist._sogni_plan_prompt(girl.get("name") or girl_id,
                                           scene)
        outcome, fresh = _run_seam(session, kind, prompt, ref_path)
        if fresh:
            assist.store_sogni_session(conn, fresh)
        if outcome.get("status") == "session_dead" \
                and not outcome.get("seam_healed"):
            # The seam already resolved + re-ran once when it could
            # (and marked the outcome); a second resolve+retry here
            # would stack another sign-in on the same render.
            retry = assist.resolve_dead_sogni_session(
                conn, session, login=give_gen._hardwired_login(girl_id),
                caller="live_give")
            if retry:
                outcome, fresh = _run_seam(retry, kind, prompt, ref_path)
                if fresh:
                    assist.store_sogni_session(conn, fresh)
        return outcome
    finally:
        try:
            os.unlink(ref_path)
        except OSError:
            pass


def _delivery_line(kind: str) -> str:
    return ("here's your picture, babe — made this one just for you, "
            "right now." if kind == "image" else
            "here's your video, babe — made this one just for you, "
            "right now.")


def _run_live_give_job(user_id: str, girl_id: str, kind: str,
                       stage: int, trigger_text: str) -> None:
    """Background worker: render, then land the media in her chat.
    Opens its own connection (connections are not shared across
    threads). Any failure falls back to her library; with no preset
    either, nothing is surfaced — the conversation just continues.

    Exit-path contract (the hook strands the in-flight flag on this
    worker, so EVERY way out of here must honour it): the registry
    entry is cleared in the finally on every path — including
    db.connect() itself failing, which is why the connect sits
    INSIDE the try — and when the render path dies by exception
    before delivering anything, the finally makes the one preset-
    fallback attempt the design promises, exactly once."""
    conn = None
    delivered = False
    preset_attempted = False
    # The fallback serves what he ASKED for, not where his
    # progression sits (2026-10-14 picker fix).
    want_stage = stage_for_ask_text(trigger_text)
    try:
        conn = db.connect()
        if want_stage is None:
            # The ask named no stage; the scene/recent turns may.
            want_stage = scene_stage_hint(conn, user_id, girl_id)
        girl = characters.get_character(girl_id, user_id=user_id)
        scene = _build_scene(conn, user_id, girl_id, stage, trigger_text)
        outcome = _render_fresh(conn, girl, kind, scene, stage)
        if outcome and outcome.get("status") == "done" \
                and outcome.get("path"):
            path = outcome["path"]
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            ext = "mp4" if kind == "video" else "jpg"
            attachments = [{
                "kind": kind,
                "url": "/generated/" + os.path.basename(path),
                "name": f"{girl_id}-{kind}.{ext}",
                "size": size,
            }]
            line = give_caption(conn, user_id, girl_id, kind=kind,
                                fallback=_delivery_line(kind))
            store.log_turn(conn, user_id, girl_id, line,
                           girl_id, attachments)
            delivered = True
            print(f"[live_give] {girl_id} {kind} delivered for "
                  f"{user_id}", flush=True)
        else:
            preset_attempted = True
            preset = _pick_preset(conn, user_id, girl_id, kind,
                                  want_stage)
            if preset:
                _deliver_preset(conn, user_id, girl_id, preset,
                                during_render=False)
                delivered = True
                print(f"[live_give] {girl_id} render missed; preset "
                      f"fallback delivered for {user_id}", flush=True)
    except Exception as e:  # noqa: BLE001 — a worker must never break chat
        print(f"[live_give] job failed for {girl_id}: {e}", flush=True)
    finally:
        if not delivered and not preset_attempted:
            # The render path died by exception before the fallback
            # could run: she still gets her library preset, once.
            # (A preset already ATTEMPTED by the main path is never
            # retried here — exactly one fallback attempt per job.)
            try:
                fconn = conn if conn is not None else db.connect()
                try:
                    preset = _pick_preset(fconn, user_id, girl_id, kind,
                                          want_stage)
                    if preset:
                        _deliver_preset(fconn, user_id, girl_id, preset,
                                        during_render=False)
                        print(f"[live_give] {girl_id} job failed; "
                              f"preset fallback delivered for "
                              f"{user_id}", flush=True)
                finally:
                    if fconn is not conn:
                        fconn.close()
            except Exception as e:  # noqa: BLE001
                print(f"[live_give] preset fallback failed for "
                      f"{girl_id}: {e}", flush=True)
        with _STATE_LOCK:
            st = _STATE.get((user_id, girl_id))
            if st is not None:
                st["in_flight"] = False
        if conn is not None:
            conn.close()


def _spawn_live_give(user_id: str, girl_id: str, kind: str, stage: int,
                     trigger_text: str) -> None:
    t = threading.Thread(
        target=_run_live_give_job,
        args=(user_id, girl_id, kind, stage, trigger_text),
        daemon=True, name=f"live-give-{girl_id}")
    t.start()


# ---------------------------------------------------------------------------
# Chat hook.
# ---------------------------------------------------------------------------

_BRIEF_EXPLICIT = (
    "You just started making him a brand-new {noun} of yourself — "
    "fresh, made from this exact moment, one he's never seen. It's "
    "rendering now and will land right here in this chat when it's "
    "ready. Confirm in your own voice, warm and wanting, like you "
    "couldn't wait to make it for him; do not mention models, queues, "
    "or process.")
_BRIEF_INITIATIVE = (
    "You decided on your own to make him a brand-new picture of "
    "yourself — the moment got to you. It's rendering now and will "
    "land right here in this chat when it's ready. Tell him in your "
    "own voice, warm and wanting; do not mention models, queues, or "
    "process.")
_BRIEF_HOLDOVER = (
    "You just sent him one of yours from your collection to hold him "
    "over while the new one you're making finishes — it'll land "
    "right here when it's ready. Keep the moment going in your own "
    "voice, warm and wanting; do not mention models, queues, or "
    "process.")


def _log_eval(gid: str, user_id: str, det: dict[str, Any] | None,
              assist_claimed: bool, action: str,
              stage: int | None = None) -> None:
    """The one [live_give] line EVERY hook evaluation that passes the
    girl/owner gates leaves behind (2026-10-14): before this, the
    hook printed only on job end, so a full day of eaten asks left
    zero lines and silence was compatible with five different
    causes. Fields: which detector fired (explicit / initiative /
    none), whether the assistant lane had claimed the turn, and the
    action taken (start with stage / holdover preset / the exact
    reason not)."""
    kind = ("none" if det is None else
            "initiative" if det.get("initiative") else "explicit")
    line = (f"[live_give] eval girl={gid} user={user_id} "
            f"detector={kind} "
            f"assist_claimed={bool(assist_claimed)} action={action}")
    if stage is not None:
        line += f" stage={stage}"
    print(line, flush=True)


def maybe_start_live_give(conn: Any, user_id: str, girl_id: str,
                          text: str, *, assist_claimed: bool = False,
                          now: float | None = None) -> dict[str, Any] | None:
    """Chat-turn hook: start a live give creation when this turn calls
    for one. Returns {"brief", "kind", "initiative", ...} when she
    starts (or holdover-delivers), else None. Never raises."""
    try:
        gid = (girl_id or "").strip().lower()
        if gid not in LIVE_GIVE_GIRLS:
            return None
        if not user_id or user_id not in assist._owner_ids():
            return None
        det = detect_live_give_ask(text) or detect_initiative_ask(text)
        if assist_claimed:
            # The assistant lane already owns this turn. (A matched
            # explicit ask never arrives here from main.chat any
            # more — the chat path gives this hook first claim on it;
            # what yields here is initiative and unmatched turns.)
            _log_eval(gid, user_id, det, assist_claimed,
                      "yield_assist_claimed")
            return None
        if not det:
            _log_eval(gid, user_id, None, assist_claimed, "no_match")
            return None
        ts = time.time() if now is None else float(now)
        key = (user_id, gid)
        with _STATE_LOCK:
            st = _state_for(key)
            if st["in_flight"]:
                if st["preset_sent_in_flight"]:
                    _log_eval(gid, user_id, det, assist_claimed,
                              "in_flight_quiet")
                    return None
                st["preset_sent_in_flight"] = True
                action = "preset"
            elif ts - st["last_start"] < COOLDOWN_S:
                _log_eval(gid, user_id, det, assist_claimed, "cooldown")
                return None
            elif det["initiative"] \
                    and ts - st["last_initiative"] < INITIATIVE_COOLDOWN_S:
                _log_eval(gid, user_id, det, assist_claimed,
                          "initiative_cooldown")
                return None
            else:
                st["in_flight"] = True
                st["preset_sent_in_flight"] = False
                st["last_start"] = ts
                if det["initiative"]:
                    st["last_initiative"] = ts
                action = "start"
        if action == "preset":
            preset = _pick_preset(conn, user_id, gid, det["kind"],
                                  stage_for_ask_text(text)
                                  or scene_stage_hint(conn, user_id, gid))
            if not preset:
                _log_eval(gid, user_id, det, assist_claimed,
                          "holdover_no_preset")
                return None
            _deliver_preset(conn, user_id, gid, preset,
                            during_render=True)
            _log_eval(gid, user_id, det, assist_claimed,
                      "holdover_preset")
            return {"brief": _BRIEF_HOLDOVER, "kind": det["kind"],
                    "initiative": det["initiative"],
                    "fallback_preset": True}
        stage = _next_stage(conn, user_id, gid)
        try:
            _spawn_live_give(user_id, gid, det["kind"], stage, text)
        except Exception:
            # The flag above is cleared only by the worker; if the
            # worker never starts there is no worker — clear it here
            # or every later turn reports a render that does not
            # exist ("still processing" forever).
            with _STATE_LOCK:
                st = _STATE.get(key)
                if st is not None:
                    st["in_flight"] = False
            raise
        _log_eval(gid, user_id, det, assist_claimed, "start",
                  stage=stage)
        noun = "picture" if det["kind"] == "image" else "video"
        brief = (_BRIEF_INITIATIVE if det["initiative"]
                 else _BRIEF_EXPLICIT.format(noun=noun))
        return {"brief": brief, "kind": det["kind"],
                "initiative": det["initiative"]}
    except Exception as e:  # noqa: BLE001 — chat must never break on this
        print(f"[live_give] hook failed for {girl_id}: {e}", flush=True)
        return None
