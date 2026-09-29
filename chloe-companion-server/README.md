# Chloe — AI Companion (Phase 2 backend scaffold)

A brand-new, standalone service for the **Chloe AI Companion** product
(persistent 1-on-1 companion, subscription-billed). Phase 2 = design +
working skeleton. **Nothing here touches production** — not the
`keyhole-site` repo, not the live backend, not DNS, not Stripe live mode.

Stack: **Python / FastAPI** + SQLite (stdlib `sqlite3`), matching the
existing backend's language.

## Quick start

```bash
cd ~/workspace/chloe-companion-server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8100
# API at http://127.0.0.1:8100  —  /health for a liveness check
```

Run the tests (memory-isolation proof included):

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## API contract (exactly as specified)

| Method | Path | Body / Query | Returns |
|---|---|---|---|
| POST | `/api/v1/chat` | `{user_id, message}` | `{reply_text, voice_note_url\|null, photo_url\|null, messages_remaining}` |
| GET | `/api/v1/me?user_id=` | — | `{user_id, name, tier, messages_remaining, voice_notes_remaining, proactive_enabled}` |
| GET | `/api/v1/shows` | — | `{shows:[{id,title,starts_at,ticket_url}]}` |
| POST | `/api/v1/memory/export` | `{user_id}` | `{memories:[...]}` |
| POST | `/api/v1/memory/delete` | `{user_id}` | `{ok:true}` |

Extras the brain needs (not in the contract, harmless): `POST
/api/v1/memory/write {user_id, mem_type, text}` and `POST
/api/v1/stripe/webhook`.

Chat behavior:
- Each reply decrements the message bank by 1.
- At zero: graceful refusal with an upsell nudge, `messages_remaining: 0`.
  Never crashes, never goes negative.
- Voice notes: tier-gated (`plus`/`vip` only) and STUBBED — see below.
- Photo drops: tier-gated; every 7th user message pulls the next eligible
  drop from `photo_drops/manifest.json` (currently 2 placeholders).

## What is working vs stubbed

**Working (real logic, tested):**
- Per-user isolated memory: typed memories (`core`/`fact`/`preference`/
  `boundary`/`tone`), recall with type/keyword filters, JSON export, full
  delete. `user_id` is bound into every query by construction; the test
  suite proves user A's rows never surface for user B.
- Message/voice banks: decrement per reply, graceful empty-bank refusal,
  monthly top-up **to** the allocation (no stacking).
- Companion brain pipeline: `mouth -> auditor`. The system prompt is built
  from the user-written character spec in
  `~/workspace/goals/chloe-ai-companion/GOAL.md` (warm, present, accepts
  him, has a self, never punishes; warm/short/real voice).
- Proactive scheduler: good-morning / missed-you / show-hype, max 2/day per
  user, quiet hours 11pm–8am user-local, tier-gated to $19.99+. Dry-run by
  default (writes `proactive_queue.json`).
- Photo-drop manifest schema + tier-gated serving hook.
- Shows stub (`shows.json`) + serving endpoint; companion can mention them.
- Stripe webhook handler logic (created/updated → top up banks, no stacking;
  deleted → noted). Definitions only — see below.

**Stubbed (clearly labeled, no live wiring):**
- **Reply generator**: canned warm/short replies behind the `Mouth`
  interface. The real LLM plugs into `Mouth.generate()`; the slower
  reasoning auditor plugs into `audit_reply()` in `app/brain.py` (currently
  a pass-through, always in the pipeline).
- **Liora voice notes (xAI TTS)**: DISABLED. Returns `null` unless
  `CHLOE_XAI_API_KEY` is set AND `CHLOE_VOICE_ENABLED=1`. Even then, Phase 2
  makes **no network calls** — paid TTS needs the user's explicit approval
  before any real use.
- **Stripe**: product/price IDs in `app/config.py` are **test-mode
  placeholders** (`prod_TEST_PLACEHOLDER_*`). No live keys, no real
  charges, ever. Webhook skips signature verification (Phase 3 must add it).

## File tree

```
app/
  main.py        FastAPI app + API contract + Stripe webhook stub
  config.py      tiers, Stripe test placeholders, feature flags
  db.py          SQLite schema (users / memories / conversation)
  store.py       per-user memory + bank store (isolation enforced here)
  brain.py       character system prompt, Mouth stub, auditor hook
  voice.py       xAI TTS stub (disabled by default)
  photos.py      photo-drop manifest + tier-gated serving hook
  shows.py       shows.json reader
  scheduler.py   proactive scheduler (dry-run queue, caps, quiet hours)
photo_drops/manifest.json   2 PLACEHOLDER drops (user curates real photos later)
shows.json       2 stub shows
tests/test_memory_isolation.py   isolation proof + bank + scheduler-guard tests
requirements.txt / .gitignore / README.md
```

## Pricing tiers (from GOAL.md)

- **$9.99/mo** — 300 messages (text only)
- **$19.99/mo** — 1,000 messages + 60 voice notes + photo drops + proactive
- **$39.99/mo** — 2,500 messages + 150 voice notes + photo drops + proactive + presale perks

Banks refill to the allocation each billing cycle; they never stack.

## Explicit content line (per product decision 2026-09-28)

Explicit video performances stay on the **website only**. The companion is
warm/flirty in conversation at most — never explicit, never punished for
asking, never a salesperson.
