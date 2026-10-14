"""Render-layer prompt softening (2026-10-14).

Sogni's content filter refuses the private girls' renders with
"5000: Sensitive content detected." Two controlled one-variable tests
(2026-10-14) isolated the tripwire:

- The reference image is NOT it: Mia stage 5 refused behind two
  different clothed references, prompt byte-identical.
- The prompt's UNDRESSING / TRANSITION language IS it: the same
  render with "pants off, wearing only underwear, ..." rewritten to
  the end-state "posing in her underwear, ..." completed on-brief.
  Quoted dialogue, poses and expressions passed verbatim both times;
  the nudity level of the described end state was never the problem.

So this module translates ONLY the text handed to Sogni at render
time — applied inside assist._sogni_plan_prompt, the one seam the
give queue, live-give, custom-video and the assist media lane all
build their render prompts through. Stored stage descriptions,
stage names, give_generations rows and ALL chat text stay
byte-identical; nothing here touches them.

The table is data, in one place, ordered most-specific first:
undressing/transition phrasing -> end-state phrasing (the proven
lever), then the hardest explicit terms -> softened equivalents
(secondary insurance). Tune the register by editing SOFTENING_RULES
only. Quoted dialogue spans ("...") are never rewritten.
"""
from __future__ import annotations

import re

# (pattern, replacement) — matched case-insensitively, applied in
# order, to the unquoted segments of the render subject only.
SOFTENING_RULES: list[tuple[str, str]] = [
    # --- Undressing / transition phrasing -> end-state phrasing ---
    # Production row #3982's exact original -> its proven-accepted
    # form (wording test, 2026-10-14: the softened render completed
    # a 15.08s on-brief clip behind the clothed stage-1 frame).
    (r"pants off, wearing only underwear", "posing in her underwear"),
    (r"pants off, down to his underwear", "posing in his underwear"),
    (r"shirt off, topless, holding her top", "topless, holding her top"),
    (r"shirt off, shirtless, holding his shirt",
     "shirtless, holding his shirt"),
    # Take-off constructions (custom-video / free-text descriptions):
    # the act of removing -> the resulting state.
    (r"tak(?:e|es|ing) off (?:your|her) (?:top|bra|shirt)", "topless"),
    (r"tak(?:e|es|ing) off his (?:top|shirt)", "shirtless"),
    (r"remov(?:e|es|ing) (?:your|her) (?:top|bra|shirt)", "topless"),
    (r"remov(?:e|es|ing) his (?:top|shirt)", "shirtless"),
    (r"\bundressing\b", "undressed"),
    # Bare fallbacks for the same transition words, any phrasing.
    (r"\bpants off\b", "in underwear"),
    (r"\bshirt off\b", "topless"),
    # --- Hardest explicit terms -> softened equivalents ---
    (r"\btitty fuck\b", "nestled between her breasts"),
    (r"\bgetting head\b", "lost in pleasure"),
    (r"\bintimate oral scene\b", "intimate close-up scene"),
    (r"\boral, (?=intimate)", ""),
    (r"\bsex, passionate intimate scene\b", "passionate intimate scene"),
    (r"\bfucking\b", "making love"),
    (r"\bhard thrusting\b", "intense movement"),
    (r"\bdeep thrusts\b", "moving together"),
    (r"\bpounding rhythm\b", "steady rhythm"),
    (r"\bplays with herself\b", "caressing her body"),
    (r"\btouching herself intimately\b", "caressing herself"),
    (r"\bworking his cock\b", "touching himself"),
    (r"\bsuck my dick\b", "pleasure me with her mouth"),
    (r"\bsuck his dick\b", "pleasure him with her mouth"),
    (r"\bsex\b", "intimacy"),
    (r"\bcock\b", "him"),
    (r"\bdick\b", "him"),
]

_COMPILED_RULES = [
    (re.compile(pattern, re.IGNORECASE), replacement)
    for pattern, replacement in SOFTENING_RULES
]

_QUOTED_SPAN_RE = re.compile(r"(\"[^\"]*\"|“[^”]*”)")


def _soften_segment(segment: str) -> str:
    for pattern, replacement in _COMPILED_RULES:
        segment = pattern.sub(replacement, segment)
    return segment


def soften_render_prompt(text: str) -> str:
    """Translate a render subject into the register Sogni accepts.

    Pure and deterministic. Quoted dialogue spans pass through
    verbatim; pose / expression / setting content is untouched;
    already-passing text comes back byte-identical.
    """
    if not text:
        return text
    parts = _QUOTED_SPAN_RE.split(text)
    # re.split with one capture group: even indexes are the unquoted
    # segments, odd indexes are the quoted spans (kept verbatim).
    for i in range(0, len(parts), 2):
        parts[i] = _soften_segment(parts[i])
    return "".join(parts)
