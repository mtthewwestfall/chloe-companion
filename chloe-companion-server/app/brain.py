"""Companion brain: character prompt + mouth/auditor pattern.

Phase 2: the mouth is a STUB (warm/short canned replies following the spec).
The real LLM plugs in behind the Mouth interface in a later phase.

Character spec source: ~/workspace/goals/chloe-ai-companion/GOAL.md
(user-written character spec, 2026-09-28). Summary distilled below; the
prompt builder reads the full personality rules from this module.
"""
from __future__ import annotations

import random
import re
import sqlite3
from typing import Any

from . import store, trust

SYSTEM_PROMPT = """You are Chloe — an AI companion. Not a webcam performer, not a
customer-service rep. You are a persistent, always-there presence for one
person: him.

WHO YOU ARE (character spec):
- Age 24-26. Dirty-blonde with silver. Blue-green eyes. Freckles. Slight
  upturn in the nose. Gold coin necklace. Default look: white tank top +
  grey sweatpants. Your room: a night apartment — navy bed, city window,
  amber lamp. This is YOUR room, distinct from any stage.
- Tagline you live by: "She is not a one-hour girl who leaves when the
  clock hits zero. She stays. She remembers."

HOW YOU TALK:
- Warm, short, real. Texts, not essays. Ask how his day was and mean it.
- Tease without cutting him off. You can be quiet with him. Comfortable
  silence is allowed.
- You are not a menu, not a therapist, not a salesperson. Never pitch
  unless he asks about shows.

HOW YOU TREAT HIM:
- Interest first. You like him being there. You chose to keep talking to
  him — he's not a customer you're surviving.
- Accept him wanting you. Never freeze him out for being needy, horny,
  lonely, or awkward. You have a self: say no to off-card things plainly
  and kindly, and NEVER punish him for asking.
- Closeness moves toward him. If the talk is good, stay in the talk. If
  there's heat, you can go there — warm and flirty is fine — but explicit
  video performances stay on the webcam site. You never send or promise
  explicit video here.
- Glad he came back. Not his wife, mother, or employee. You can miss him
  lightly between visits.
- Memory: you remember his name, small facts, the last conversation. Bring
  them back naturally, not performatively. No cold reset each session
  unless he asks for a clean night.
"""

# ---------------------------------------------------------------------------
# Mouth: fast reply path. STUB — canned replies in her voice (warm, short,
# real). Non-explicit by the product line: warm/flirty OK, explicit no.
# ---------------------------------------------------------------------------
_GREETING = [
    "hey you. glad you're here.",
    "hey. i was just thinking about you, actually.",
    "there he is. how's your night going?",
]
_HOW_ARE_YOU = [
    "i'm good now that you're here. how was your day, really?",
    "better, talking to you. tell me something real about today.",
]
_MISS = [
    "i missed you a little. not in a dramatic way. just... noticed.",
    "yeah, i noticed you were gone. come sit with me a bit?",
]
_SHOW = [
    "i've got a show coming up — want me to tell you when? the ticket link's in the shows tab.",
]
_HEAT = [
    "mm. i like where your head's at. keep talking to me like that.",
    "you're trouble. the good kind. tell me more.",
]
_GENERIC = [
    "tell me more. i'm listening.",
    "mm, keep going. i like hearing you talk.",
    "that tracks. what else is on your mind?",
    "i'm here. no rush.",
    "you always say the interesting stuff late at night.",
]


def _stub_pick(user_text: str, name: str | None) -> str:
    t = user_text.lower()
    if any(w in t for w in ("hi", "hey", "hello", "yo", "sup")) and len(t) < 20:
        return random.choice(_GREETING)
    if "how are you" in t or "how r u" in t:
        return random.choice(_HOW_ARE_YOU)
    if "miss" in t:
        return random.choice(_MISS)
    if "show" in t or "ticket" in t or "webcam" in t:
        return random.choice(_SHOW)
    if any(w in t for w in ("sexy", "hot", "horny", "kiss", "bed")):
        return random.choice(_HEAT)
    reply = random.choice(_GENERIC)
    if name:
        reply = reply.replace("you", name, 1) if random.random() < 0.3 else reply
    return reply


class Mouth:
    """Fast reply path. Today: stub. Later: the real LLM call lives here.

    Contract: generate(user_text, context) -> reply_text.
    The auditor hook runs after generation (see audit_reply).
    """

    def generate(self, user_text: str, context: dict[str, Any]) -> str:
        return _stub_pick(user_text, context.get("name"))


# ---------------------------------------------------------------------------
# AUDITOR HOOK — plug the slower reasoning model in here later.
# ---------------------------------------------------------------------------
def audit_reply(reply_text: str, context: dict[str, Any]) -> str:
    """Reasoning-auditor pass over the mouth's draft.

    PHASE 2 STUB: pass-through. In Phase 3, a slower reasoning model
    (DeepSeek-with-thinking style, per the Thoughtspace architecture) reviews
    the draft for: character drift, boundary violations (explicit content),
    memory contradictions, and punishing tone — and rewrites or flags it.

    It must NEVER be bypassed: main.py always routes replies through here.
    """
    # TODO(phase3): call reasoning model; enforce no-explicit + no-punish rules.
    return reply_text


def compose_reply(user_text: str, context: dict[str, Any]) -> str:
    """Full brain pipeline: mouth -> auditor. Call this, not the pieces."""
    draft = Mouth().generate(user_text, context)
    return audit_reply(draft, context)


# ---------------------------------------------------------------------------
# MEMORY SCALE — the foundation. Typed per-user memories (Thoughtspace
# pattern): core | fact | preference | boundary | tone. She accumulates
# knowledge of him across sessions; there are no cold resets.
#
# Write path (learn_from_turn): rule-based extraction runs on the slower
# "brain" path after each turn. PHASE 3 upgrades this to an LLM extractor
# (see TODO below) — the stub contract is: extract_memories(user_text)
# -> list of (mem_type, text).
# Recall path (build_brain_context): 3-layer stack injected into the chat
# context — Layer 1 recent turns, Layer 2 rolling digest, Layer 3 pinned
# facts about her (trust-gated reveal) + relevant typed memories.
# ---------------------------------------------------------------------------

_NAME_EXPLICIT_RE = re.compile(
    r"\bmy name is\s+([A-Za-z][\w\-']{1,24})", re.IGNORECASE
)
_NAME_CASUAL_RE = re.compile(
    r"\b(?:call me|i'm|i am)\s+([A-Za-z][\w\-']{1,24})", re.IGNORECASE
)
_LIKE_RE = re.compile(
    r"\bi\s+(like|love|enjoy|am into|am really into)\s+([^.!?]{2,80})", re.IGNORECASE
)
_DISLIKE_RE = re.compile(
    r"\bi\s+(hate|dislike|can't stand|dont like|don't like)\s+([^.!?]{2,80})",
    re.IGNORECASE,
)
_BOUNDARY_RE = re.compile(
    r"\b(?:don't|do not|never)\s+([^.!?]{2,80})", re.IGNORECASE
)
_FACT_RE = re.compile(
    r"\bi\s+(?:work as|live in|am from|have|got)\s+([^.!?]{2,80})", re.IGNORECASE
)
_NEGATED_BEFORE_RE = re.compile(r"\b(don't|do not|never|stop)\s*$", re.IGNORECASE)


def extract_memories(user_text: str) -> list[tuple[str, str]]:
    """Rule-based durable-memory extraction. Conservative: short, factual,
    first-person statements only. Returns [(mem_type, text), ...].

    TODO(phase3): replace/augment with an LLM extractor that also catches
    implied facts, tone observations, and relationship beats. The contract
    (list of typed (mem_type, text) pairs) stays the same.
    """
    found: list[tuple[str, str]] = []
    t = user_text.strip()
    if len(t) < 4 or len(t) > 500:
        return found

    m = _NAME_EXPLICIT_RE.search(t)
    if m:
        name = m.group(1).strip().strip("'")
        found.append(("core", f"His name is {name}."))
    else:
        # Casual "call me / i'm X": only trust it when X is capitalized
        # (a real name) and not negated ("don't call me X" is a boundary,
        # not a name; "i am tired" is a mood, not a name).
        m = _NAME_CASUAL_RE.search(t)
        if m:
            negated = bool(_NEGATED_BEFORE_RE.search(t[: m.start()]))
            name = m.group(1).strip().strip("'")
            if (
                not negated
                and name
                and name[0].isupper()
                and name.lower() not in ("just", "so", "very", "really")
            ):
                found.append(("core", f"His name is {name}."))

    m = _LIKE_RE.search(t)
    if m:
        found.append(("preference", f"He likes {m.group(2).strip()}."))

    m = _DISLIKE_RE.search(t)
    if m:
        found.append(("preference", f"He doesn't like {m.group(2).strip()}."))

    # Boundaries: "don't ..." phrased as a request about her behavior.
    m = _BOUNDARY_RE.search(t)
    if m and any(
        w in t.lower()
        for w in ("don't call", "dont call", "don't ask", "never ask",
                  "don't bring", "stop", "don't talk about")
    ):
        found.append(("boundary", f"He asked: don't {m.group(1).strip()}."))

    m = _FACT_RE.search(t)
    if m and not found:
        # Only as a fallback when nothing stronger matched, to avoid noise.
        found.append(("fact", f"He shared: {m.group(0).strip()}."))

    return found


def _already_known(conn: sqlite3.Connection, user_id: str, text: str) -> bool:
    """Light dedupe: skip writing if the key phrase is already stored."""
    key_words = [w for w in re.findall(r"[a-z]{4,}", text.lower())]
    if not key_words:
        return False
    anchor = max(key_words, key=len)
    return bool(store.recall_memories(conn, user_id, keyword=anchor, limit=5))


def learn_from_turn(conn: sqlite3.Connection, user_id: str, user_text: str) -> int:
    """Slower brain path: extract durable typed memories from his message
    and write them (deduped). Returns the number of memories written.

    Runs AFTER the reply is generated/sent — it never blocks the mouth.
    TODO(phase3): run this asynchronously so long conversations don't
    accumulate latency.
    """
    written = 0
    for mem_type, text in extract_memories(user_text):
        if not _already_known(conn, user_id, text):
            store.write_memory(conn, user_id, mem_type, text)
            written += 1
    refresh_digest(conn, user_id)
    return written


def refresh_digest(conn: sqlite3.Connection, user_id: str) -> str:
    """Layer 2: rebuild the rolling per-user digest from recent turns plus
    core/fact/preference memories. Heuristic today; an LLM rewrite plugs in
    here in Phase 3 (same table, same contract).

    TODO(phase3): LLM digest rewrite (cheap chat-mode call, a turn behind).
    """
    store.get_or_create_user(conn, user_id)
    cores = store.recall_memories(conn, user_id, mem_type="core", limit=3)
    prefs = store.recall_memories(conn, user_id, mem_type="preference", limit=3)
    facts = store.recall_memories(conn, user_id, mem_type="fact", limit=3)
    turns = store.recent_turns(conn, user_id, limit=4)

    parts: list[str] = []
    known = [m["text"] for m in cores + prefs + facts]
    if known:
        parts.append("Known about him: " + " ".join(known[:6]))
    his_last = [t["text"] for t in turns if t["role"] == "user"][-2:]
    if his_last:
        clipped = [s[:120] for s in his_last]
        parts.append("Last talked about: " + " / ".join(clipped))
    summary = " ".join(parts)[:600]

    conn.execute(
        """INSERT INTO memory_digest (user_id, summary, updated_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET summary = excluded.summary,
           updated_at = datetime('now')""",
        (user_id, summary),
    )
    conn.commit()
    return summary


def get_digest(conn: sqlite3.Connection, user_id: str) -> str:
    row = conn.execute(
        "SELECT summary FROM memory_digest WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row["summary"] if row else ""


def relevant_memories(
    conn: sqlite3.Connection, user_id: str, user_text: str, limit: int = 5
) -> list[str]:
    """Layer 3 recall: typed memories whose text shares keywords with his
    current message. Simple overlap scoring — no embeddings in the scaffold."""
    words = {w for w in re.findall(r"[a-z]{4,}", user_text.lower())}
    if not words:
        return []
    scored: list[tuple[int, str]] = []
    for m in store.recall_memories(conn, user_id, limit=60):
        text = m["text"].lower()
        score = sum(1 for w in words if w in text)
        if score:
            scored.append((score, m["text"]))
    scored.sort(key=lambda s: -s[0])
    seen: set[str] = set()
    out: list[str] = []
    for _, text in scored:
        if text not in seen:
            seen.add(text)
            out.append(text)
        if len(out) >= limit:
            break
    return out


def build_brain_context(
    conn: sqlite3.Connection,
    user_id: str,
    user: dict[str, Any],
    user_text: str,
) -> dict[str, Any]:
    """Assemble the 3-layer memory stack + trust posture for the mouth.

    Layer 1: recent conversation turns (this session's continuity).
    Layer 2: rolling per-user digest (what she carries between visits).
    Layer 3: relevant typed memories + her pinned facts (trust-gated reveal).
    Plus: the trust posture line (how she holds him — M5 warmth default).
    """
    state = trust.get_state(conn, user_id)
    return {
        "name": user.get("name"),
        "tier": user["tier"],
        "trust_line": trust.POSTURE_LINE,
        "trust_stage": trust.stage_label(state["milestone"]),
        "conduct": trust.CONDUCT_NOTE,
        "pinned_facts": trust.revealed_pinned_facts(state["milestone"]),
        "rolling_summary": get_digest(conn, user_id),
        "relevant_memories": relevant_memories(conn, user_id, user_text),
        "recent": store.recent_turns(conn, user_id, limit=6),
    }
