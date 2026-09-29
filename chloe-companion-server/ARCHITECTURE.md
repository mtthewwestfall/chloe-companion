# Chloe Companion — Memory + Trust Architecture

Decided with the user 2026-09-28. Implemented in this server (`app/trust.py`,
`app/brain.py`, `app/store.py`, `app/db.py`).

## The split

**MEMORY SCALE is the foundation.** She gets closer because she *remembers* —
his name, small facts, last conversation, boundaries — not because he passes
gates. Typed per-user memories (`core | fact | preference | boundary | tone`,
Thoughtspace pattern) accumulate across sessions. No cold resets: the rolling
digest and conversation log persist until he asks for a clean night
(`POST /api/v1/memory/delete`).

**TRUST is POSTURE, not a gate.** Borrowed from the Sorority House trust
engine, but re-scoped:

- Every new user starts at **M5 ("Trusted", ~58/100)** — never M1. She treats
  him as already someone she likes from the first message. Warmth,
  availability, and flirtation are **never** gated by trust.
- The upper climb **M5 → M6 → M7 → M8** ("Trusted" → "Confided" → "Confided"
  → "Different") only gates how much of **herself** she volunteers — pinned
  self-facts revealed per stage. Deepening, not proving. There is no
  audition phase; the M1–M4 grind is skipped entirely.

## Tunable dials (all in `app/trust.py`)

| Dial | Current | Meaning |
|---|---|---|
| `START_MILESTONE` | `5` | New users never start below M5. |
| `STAGE_DAYS` | `[2, 3, 5]` | Min **real** days at M5/M6/M7 before deepening to M6/M7/M8. Short on purpose — a paying subscriber should feel the relationship move within weeks, not seasons. |
| `CHLOE_PINNED` | 20 facts | Her pinned self-facts, grouped by unlock stage. M5 = everyday surface (apartment, necklace, tank/sweats); M6–M8 = deeper texture (habits → small vulnerabilities → what "staying" means to her). All tasteful, non-explicit, girlfriend-warm. |
| `CONDUCT_NOTE` / `POSTURE_LINE` | — | Injected into chat context: how she holds him (warm from message one, never punishes asking, moves toward closeness). |

`maybe_advance()` checks the stage clock once per chat turn and advances **at
most one** milestone per check. M8 is terminal.

## The 3-layer memory stack (per chat turn)

1. **Layer 1 — recent turns** (`store.recent_turns`): this session's continuity.
2. **Layer 2 — rolling digest** (`memory_digest` table, one row per user):
   rebuilt after every turn by `brain.refresh_digest()` — currently a
   heuristic (known facts + last topics); a cheap LLM rewrite plugs in here
   in Phase 3, same table, same contract.
3. **Layer 3 — typed memories + her pinned facts**: `brain.relevant_memories()`
   scores stored memories by keyword overlap with his current message;
   `trust.revealed_pinned_facts()` returns her cumulative unlocked self-facts.

All of it lands in the context dict passed to the mouth (`Mouth.generate`),
alongside `trust_line`, `trust_stage`, and `conduct`.

## Write path (the slower "brain")

After the reply is sent, `brain.learn_from_turn()` runs rule-based extraction
(`brain.extract_memories`): name statements → `core`, likes/dislikes →
`preference`, "don't …" requests → `boundary`, "I work/live/am from …" →
`fact`. Conservative by design (capitalized names only, negation guards,
dedupe against what's already stored). **Phase 3** replaces/augments this
with an LLM extractor — the `(mem_type, text)` contract stays the same.
Marked `TODO(phase3)` in code; not faked.

## Isolation

Every table is keyed by `user_id`; every store/trust/brain function takes
`user_id` as a required argument. Trust state, digests, memories, and
conversation logs can never cross users (tested: `TrustIsolationTest`,
`BrainExtractionTest`, `MemoryIsolationTest`).

## What's still stubbed (not faked)

- Mouth replies (canned, warm/short), auditor pass-through (`audit_reply`).
- xAI/Liora TTS, real LLM, Stripe live products, push delivery.
- The photo-drop manifest still needs the user's curated final set.
