"""Prompt Studio — the owner's private prompt desk (2026-10-17).

A standalone page at /studio, OWNER-ONLY in everything but the shell.
GET /studio serves the page UI to anyone — it carries no data, no
roster, no secrets — and the page's own JS presents the app's session
token on every /api/v1/studio/* call: localStorage 'whispr_token',
read exactly the way the companion app's own getToken() reads it and
sent as 'Authorization: Bearer <token>', the app's real door. All
data/action endpoints stay gated server-side by require_studio_access
(an owner account session, ASSIST_OWNER_IDS, or the admin token via
header or ?admin_token= — the standing fallback door). A plain browser
navigation sends neither, which is why the session check lives in the
page's fetches, not in the page load: with no valid token found
client-side, the page shows the plain "Owner only." notice instead of
the studio; a signed-in owner just gets the studio.

What it is: a standing desk for his image work. A Builder assembles a
prompt from structured fields (subject, background, composition, style
presets, palette, details) with one-click templates; Generate runs the
assembled prompt — VERBATIM, this module adds no filtering, moderation
or rewriting of its own — through his Gemini key pool
(app/gemini_keys.post_json, model gemini-2.5-flash-image) with the most
permissive safety settings the API accepts (BLOCK_NONE on all four harm
categories, mirroring the companion chat's configuration). Edit mode
takes one reference image: an owner upload, or a girl's roster avatar
resolved server-side exactly like POST /api/v1/admin/image-renders.

Outputs land in config.GENERATED_DIR (durable, /data-backed) and are
served at /generated/<file>; a JSON sidecar per image feeds the page's
Recent Generations strip. Generation happens ONLY on an explicit
Generate call from the page — one pool call per tap, never in the
background.

When the MODEL refuses (its own layer — Google's, not ours), the
endpoint answers 422 with the refusal reason and the page shows it
plainly. A pool failure answers 502. No silent failures, no fake
successes.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import time

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import assist, characters, config, db, gemini_keys, store

IMAGE_MODEL = "gemini-2.5-flash-image"
_IMAGE_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              + IMAGE_MODEL + ":generateContent")
SOURCE = "prompt-studio"

# The most permissive safety settings the generateContent API accepts:
# BLOCK_NONE on every harm category, the same stance the companion chat
# ships with (app/brain.py). Sent explicitly on every studio request —
# the pool's _ensure_safety_settings only fills defaults when the
# caller sent none, so these are what the image route receives.
SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

CAMERA_ANGLES = {
    "default": "",
    "eye-level": "shot at eye level",
    "low-angle": "shot from a low angle",
    "high-angle": "shot from a high angle",
    "birds-eye": "shot from directly overhead, bird's-eye view",
    "close-up": "a close-up shot",
    "wide": "a wide shot",
}

FRAMINGS = {
    "default": "",
    "centered": "subject centered in the frame",
    "rule-of-thirds": "composed on the rule of thirds",
    "symmetrical": "a symmetrical composition",
    "off-center": "subject placed off-center",
}

# id -> (label, style phrase woven into the assembled prompt)
STYLE_PRESETS = {
    "photorealistic": (
        "Photorealistic",
        "Photorealistic — indistinguishable from a real photograph, "
        "natural texture, true-to-life lighting."),
    "illustration": (
        "Illustration",
        "Digital illustration with a hand-drawn feel and clean linework."),
    "3d-render": (
        "3D Render",
        "High-quality 3D render, CGI, soft studio lighting."),
    "watercolor": (
        "Watercolor",
        "Soft, fluid watercolor painting."),
    "oil-painting": (
        "Oil Painting",
        "Classic oil painting with visible canvas texture."),
    "anime": (
        "Anime",
        "Japanese anime style."),
    "pixel-art": (
        "Pixel Art",
        "Retro pixel art."),
    "sketch": (
        "Sketch",
        "Pencil and charcoal sketch."),
    "book-painted": (
        "Book illustration — painted",
        "Painted book illustration — warm painterly storybook style, "
        "soft visible brushwork, rich color."),
    "companion-portrait": (
        "Companion portrait",
        "An intimate companion portrait photograph — warm and personal, "
        "soft indoor light, shallow depth of field."),
    "boudoir": (
        "Boudoir / lingerie",
        "Boudoir photography — elegant lingerie, a sensual portrait, "
        "soft window light, tasteful and confident."),
    "roof-poster": (
        "Roof Rover poster",
        "Bold promotional poster art for a roofing company — clean "
        "graphic layout, strong shapes, room for a headline."),
    "missing-plate": (
        "Missing-poster plate",
        "A printed MISSING poster pinned to a wooden pole, documentary "
        "photograph, weathered paper."),
}

# One-click starters: template id -> (label, builder fields)
TEMPLATES = {
    "portrait": ("Portrait", {
        "mode": "new",
        "subject": "A portrait of a young woman with a gentle smile, "
                   "looking at the camera",
        "background": "A softly lit indoor background, blurred",
        "camera_angle": "close-up", "framing": "centered",
        "style": "companion-portrait", "palette": "", "details": "",
    }),
    "book-scene": ("Book scene", {
        "mode": "new",
        "subject": "Two college girls talking on the steps of an old "
                   "brick dormitory at dusk",
        "background": "A small college campus in autumn, leaves on the "
                      "ground, warm light in the windows",
        "camera_angle": "wide", "framing": "rule-of-thirds",
        "style": "book-painted", "palette": "warm amber and deep green",
        "details": "",
    }),
    "roof-poster": ("Roof Rover poster", {
        "mode": "new",
        "subject": "A friendly cartoon dog mascot in a roofer's cap "
                   "giving a thumbs-up beside a house with a worn roof",
        "background": "A bright suburban street, clear sky",
        "camera_angle": "eye-level", "framing": "centered",
        "style": "roof-poster", "palette": "navy, white and warm orange",
        "details": "Leave clean empty space at the top for a headline.",
    }),
    "product-shot": ("Product shot", {
        "mode": "new",
        "subject": "A studio product photograph of a single object on a "
                   "plain background",
        "background": "A seamless light-grey studio backdrop",
        "camera_angle": "eye-level", "framing": "centered",
        "style": "photorealistic", "palette": "",
        "details": "Soft shadows, no text, no people.",
    }),
}


def _clean(value) -> str:
    return " ".join(str(value or "").split()).strip()


def _sentence(text: str) -> str:
    text = text.rstrip()
    return text if text.endswith((".", "!", "?")) else text + "."


def assemble_prompt(fields: dict | None) -> str:
    """Assemble the builder fields into one prompt string.

    Deterministic and shared: the page mirrors this exact assembly for
    its live preview, and the generate endpoint assembles server-side
    from fields whenever no explicit prompt text was sent. The result
    is passed to the model verbatim — nothing here softens, filters or
    rewrites the owner's words.
    """
    f = fields or {}
    subject = _clean(f.get("subject"))
    if not subject:
        return ""
    parts = [_sentence(subject)]
    background = _clean(f.get("background"))
    if background:
        parts.append(_sentence(background))
    comp = []
    angle = CAMERA_ANGLES.get(_clean(f.get("camera_angle")) or "default", "")
    framing = FRAMINGS.get(_clean(f.get("framing")) or "default", "")
    if angle:
        comp.append(angle)
    if framing:
        comp.append(framing)
    if comp:
        parts.append("Composition: " + ", ".join(comp) + ".")
    style = STYLE_PRESETS.get(_clean(f.get("style")) or "", ("", ""))[1]
    if style:
        parts.append(style)
    palette = _clean(f.get("palette"))
    if palette:
        parts.append(_sentence("Color palette: " + palette))
    details = _clean(f.get("details"))
    if details:
        parts.append(_sentence(details))
    prompt = " ".join(parts)
    if (_clean(f.get("mode")) or "new") == "edit":
        prompt = "Edit the reference image: " + prompt
    return prompt


def build_image_payload(prompt: str, ref: bytes | None = None,
                        ref_mime: str = "image/png") -> dict:
    """One generateContent payload for the image model: the prompt
    verbatim, the reference image (edit mode) as an inlineData part,
    and BLOCK_NONE safety settings on all four harm categories."""
    parts: list[dict] = [{"text": prompt}]
    if ref:
        parts.append({"inlineData": {
            "mimeType": ref_mime,
            "data": base64.b64encode(ref).decode("ascii")}})
    return {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        "safetySettings": [dict(s) for s in SAFETY_SETTINGS],
    }


class ModelRefused(Exception):
    """The model itself declined the generation (Google's layer)."""

    def __init__(self, reason: str, model_text: str = ""):
        super().__init__(reason)
        self.reason = reason or "unknown"
        self.model_text = model_text or ""


def parse_image_response(data) -> tuple[bytes, str]:
    """Extract (image_bytes, mime) from a generateContent response.

    Raises ModelRefused with the plain reason when the model returned
    no image — promptFeedback.blockReason, a non-STOP finishReason, or
    a text-only answer — so the caller can show the refusal as-is.
    """
    if not isinstance(data, dict):
        raise ModelRefused("empty_response")
    feedback = data.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise ModelRefused(str(feedback["blockReason"]),
                           str(feedback.get("blockReasonMessage") or ""))
    for cand in data.get("candidates") or []:
        text_bits: list[str] = []
        for part in ((cand.get("content") or {}).get("parts")) or []:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            if inline.get("data"):
                return (base64.b64decode(inline["data"]),
                        str(inline.get("mimeType") or "image/png"))
            if part.get("text"):
                text_bits.append(str(part["text"]))
        finish = str(cand.get("finishReason") or "")
        if finish and finish != "STOP":
            raise ModelRefused(finish, " ".join(text_bits))
        if text_bits:
            raise ModelRefused("no_image_returned", " ".join(text_bits))
    raise ModelRefused("no_image_returned")


# ---------------------------------------------------------------------------
# Gate for every data/action endpoint: admin token OR owner account
# session — nobody else. (The /studio shell itself is public; see the
# module docstring.)
# ---------------------------------------------------------------------------
def _conn():
    return db.connect()


def _session_user_id(request: Request) -> str | None:
    token = ""
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        token = authz[7:].strip()
    if not token:
        token = (request.query_params.get("session", "") or "").strip()
    if not token:
        return None
    conn = _conn()
    try:
        return store.get_session_user_id(conn, token)
    finally:
        conn.close()


def require_studio_access(request: Request) -> str:
    """Return who got in ("admin" or the owner's user_id).

    401 when no usable credential arrived, 503 when the admin door is
    unconfigured AND no owner session arrived, 403 for a signed-in
    non-owner — a non-owner can neither load nor use the studio.
    """
    provided = (request.headers.get("x-admin-token", "")
                or request.query_params.get("admin_token", "") or "")
    expected = config.ADMIN_TOKEN
    if provided and expected and hmac.compare_digest(provided, expected):
        return "admin"
    user_id = _session_user_id(request)
    if user_id:
        if user_id in assist._owner_ids():
            return user_id
        raise HTTPException(status_code=403, detail="owner_only")
    if provided and not expected:
        raise HTTPException(status_code=503, detail="admin_disabled")
    raise HTTPException(status_code=401, detail="login_required")


# ---------------------------------------------------------------------------
# References (edit mode)
# ---------------------------------------------------------------------------
def _decode_reference_upload(image_b64: str) -> tuple[bytes, str]:
    """An owner-uploaded reference picture -> (bytes, mime)."""
    raw = (image_b64 or "").strip()
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="reference_invalid")
    if len(data) < 1024:
        raise HTTPException(status_code=400, detail="reference_invalid")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data, "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return data, "image/webp"
    return data, "image/jpeg"


def _girl_reference(girl_id: str) -> tuple[bytes, str]:
    """A girl's roster avatar as (bytes, mime), resolved server-side
    exactly like the admin image-renders endpoint: her avatar_url goes
    through assist._fetch_first_frame (a still frame for video avatars)
    and the temp file is read and removed here. A girl with no saved
    reference (Chloe — her roster avatar_url is empty) gets a plain
    reference_unavailable so the page can say: upload her photo."""
    gid = (girl_id or "").strip().lower()
    roster = {str(c.get("id") or "").lower(): c
              for c in characters.get_characters(include_private=True)}
    char = roster.get(gid)
    if char is None:
        raise HTTPException(status_code=400, detail="unknown_character")
    ref_path = assist._fetch_first_frame(char.get("avatar_url") or "")
    if not ref_path:
        raise HTTPException(status_code=400, detail="reference_unavailable")
    try:
        with open(ref_path, "rb") as fh:
            data = fh.read()
    finally:
        try:
            os.unlink(ref_path)
        except OSError:
            pass
    ext = os.path.splitext(ref_path)[1].lower()
    mime = {".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")
    return data, mime


def girls_payload() -> dict:
    """The reference picker: every roster girl and whether she has a
    saved reference photo to edit from."""
    girls = []
    for c in characters.get_characters(include_private=True):
        girls.append({
            "id": c.get("id"),
            "name": c.get("name") or c.get("id"),
            "has_reference": bool(str(c.get("avatar_url") or "").strip()),
        })
    return {"girls": girls}


# ---------------------------------------------------------------------------
# Persistence: outputs + sidecars in GENERATED_DIR, served at /generated/
# ---------------------------------------------------------------------------
_EXT_FOR_MIME = {"image/png": ".png", "image/jpeg": ".jpg",
                 "image/webp": ".webp"}


def _save_generation(img: bytes, mime: str, meta: dict) -> str:
    os.makedirs(config.GENERATED_DIR, exist_ok=True)
    stem = "studio_{0}_{1}".format(
        time.strftime("%Y%m%d%H%M%S", time.gmtime()), secrets.token_hex(3))
    fname = stem + _EXT_FOR_MIME.get(mime, ".png")
    with open(os.path.join(config.GENERATED_DIR, fname), "wb") as fh:
        fh.write(img)
    url = "/generated/" + fname
    sidecar = {**meta, "url": url,
               "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime())}
    with open(os.path.join(config.GENERATED_DIR, stem + ".json"),
              "w", encoding="utf-8") as fh:
        json.dump(sidecar, fh)
    return url


def list_recent(limit: int = 24) -> list[dict]:
    """Newest studio generations first, from GENERATED_DIR sidecars."""
    try:
        names = os.listdir(config.GENERATED_DIR)
    except OSError:
        return []
    stems = []
    for name in names:
        stem, ext = os.path.splitext(name)
        if name.startswith("studio_") and ext in (".png", ".jpg", ".webp"):
            path = os.path.join(config.GENERATED_DIR, name)
            try:
                stems.append((os.path.getmtime(path), stem, name))
            except OSError:
                pass
    stems.sort(reverse=True)
    out = []
    for _mtime, stem, name in stems[:limit]:
        entry = {"url": "/generated/" + name, "prompt": "", "mode": "",
                 "created_at": ""}
        try:
            with open(os.path.join(config.GENERATED_DIR, stem + ".json"),
                      encoding="utf-8") as fh:
                meta = json.load(fh)
            entry.update({k: meta.get(k, "") for k in
                          ("prompt", "mode", "created_at")})
        except (OSError, ValueError):
            pass
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Generate: exactly ONE pool call, only when the owner taps Generate.
# ---------------------------------------------------------------------------
def generate(body: dict) -> dict:
    prompt = _clean(body.get("prompt"))
    if not prompt:
        fields = dict(body.get("fields") or {})
        if body.get("mode"):
            fields["mode"] = body["mode"]
        prompt = assemble_prompt(fields)
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt_required")
    mode = (_clean(body.get("mode"))
            or _clean((body.get("fields") or {}).get("mode")) or "new")
    girl_id = _clean(body.get("girl_id")).lower() or None
    image_b64 = body.get("image_b64")
    if girl_id and image_b64:
        raise HTTPException(status_code=400, detail="reference_ambiguous")
    ref, ref_mime = None, "image/png"
    if mode == "edit":
        if image_b64:
            ref, ref_mime = _decode_reference_upload(image_b64)
        elif girl_id:
            ref, ref_mime = _girl_reference(girl_id)
    payload = build_image_payload(prompt, ref, ref_mime)
    data = gemini_keys.post_json(_IMAGE_URL, payload, timeout=60,
                                 source=SOURCE)
    if data is None:
        raise HTTPException(status_code=502,
                            detail="generation_failed: all_keys_failed")
    try:
        img, mime = parse_image_response(data)
    except ModelRefused as refused:
        raise HTTPException(status_code=422, detail={
            "error": "model_refused",
            "reason": refused.reason,
            "message": refused.model_text,
        })
    url = _save_generation(img, mime, {"prompt": prompt, "mode": mode,
                                       "girl_id": girl_id or ""})
    return {"ok": True, "url": url, "prompt": prompt, "mode": mode}


class StudioGenerateIn(BaseModel):
    prompt: str = ""
    fields: dict | None = None
    mode: str = ""
    girl_id: str | None = None
    image_b64: str | None = None


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------
_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prompt Studio — Whispr</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#0a0a12;color:#f5f5fa;
font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,sans-serif}
.wrap{max-width:560px;margin:0 auto;padding:20px 14px 48px}
.brand{font-size:1.6rem;font-weight:900;letter-spacing:1px;
background:linear-gradient(90deg,#ec4899,#9d4edd);-webkit-background-clip:text;
-webkit-text-fill-color:transparent}
h1{font-size:1.3rem;margin:8px 0 2px}
.sub{color:#9a9ab0;margin:0 0 16px;font-size:.95rem}
.tabs{display:flex;gap:8px;margin:0 0 14px}
.tab{flex:1;text-align:center;padding:11px;border-radius:12px;cursor:pointer;
background:#12121c;border:1px solid rgba(255,255,255,.1);color:#9a9ab0;font-weight:700}
.tab.on{color:#fff;border-color:#9d4edd;background:#1a1a2a}
.card{background:#12121c;border:1px solid rgba(255,255,255,.08);
border-radius:18px;padding:14px;margin:0 0 14px}
label{display:block;font-size:.85rem;color:#9a9ab0;margin:12px 0 5px;font-weight:600}
label:first-child{margin-top:0}
input[type=text],select,textarea{width:100%;background:#0d0d16;color:#f5f5fa;
border:1px solid rgba(255,255,255,.14);border-radius:10px;padding:11px;font-size:1rem}
textarea{min-height:74px;resize:vertical}
.row{display:flex;gap:10px}.row>div{flex:1}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}
.chip{padding:8px 12px;border-radius:999px;background:#1a1a2a;cursor:pointer;
border:1px solid rgba(255,255,255,.12);font-size:.88rem}
.chip:active{border-color:#ec4899}
.btn{display:block;width:100%;text-align:center;color:#fff;font-weight:700;
font-size:1.05rem;padding:14px 16px;border-radius:14px;border:0;cursor:pointer;
background:linear-gradient(90deg,#ec4899,#9d4edd);margin:14px 0 6px}
.btn[disabled]{opacity:.5}
.btn.ghost{background:#1a1a2a;border:1px solid rgba(255,255,255,.14)}
.status{font-size:.95rem;margin:10px 0;min-height:1.2em}
.status.err{color:#ff8fa3}.status.ok{color:#8fffc9}
#result img{width:100%;border-radius:12px;display:block}
.strip{display:flex;gap:8px;overflow-x:auto;padding:4px 0}
.strip figure{margin:0;flex:none;width:104px}
.strip img{width:104px;height:104px;object-fit:cover;border-radius:10px;display:block}
.strip figcaption{font-size:.68rem;color:#9a9ab0;margin-top:4px;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.hint{font-size:.85rem;color:#9a9ab0}
</style>
</head>
<body>
<div id="gate" style="padding:60px 20px;text-align:center">
<div class="brand">PROMPT STUDIO</div>
<h1>Prompt Studio</h1>
<p class="sub" id="gate-msg" style="margin-top:10px">Checking your sign-in…</p>
</div>
<main class="wrap" id="studio-ui" hidden>
<div class="brand">PROMPT STUDIO</div>
<h1>Your prompt desk</h1>
<p class="sub">Owner only. Prompts go to the model exactly as written.</p>

<div class="tabs">
  <div class="tab on" id="tab-builder">Builder</div>
  <div class="tab" id="tab-generate">Generate</div>
</div>

<section id="pane-builder">
  <div class="card">
    <label>Templates — one tap fills the builder</label>
    <div class="chips" id="templates"></div>
    <label for="f-mode">Generation mode</label>
    <select id="f-mode">
      <option value="new">New Image</option>
      <option value="edit">Edit Existing</option>
    </select>
    <label for="f-subject">Core subject *</label>
    <textarea id="f-subject" placeholder="Describe the main subject in detail — pose, expression, clothing for people"></textarea>
    <label for="f-background">Background &amp; setting</label>
    <textarea id="f-background" placeholder="Environment, lighting, atmosphere"></textarea>
    <div class="row">
      <div><label for="f-angle">Camera angle</label><select id="f-angle"></select></div>
      <div><label for="f-framing">Framing</label><select id="f-framing"></select></div>
    </div>
    <label for="f-style">Visual style</label>
    <select id="f-style"></select>
    <label for="f-palette">Color palette</label>
    <input type="text" id="f-palette" placeholder="e.g. warm earth tones, vibrant neon, muted pastels">
    <label for="f-details">Additional details</label>
    <textarea id="f-details" placeholder="Specific objects, textures, effects — exact wording for any text in quotes"></textarea>
  </div>
  <div class="card">
    <label for="f-prompt">Generated prompt</label>
    <textarea id="f-prompt" style="min-height:110px"></textarea>
    <button class="btn ghost" id="btn-copy" type="button">Copy prompt</button>
    <button class="btn ghost" id="btn-clear" type="button">Clear all</button>
  </div>
</section>

<section id="pane-generate" hidden>
  <div class="card" id="ref-card" hidden>
    <label>Reference image (edit mode)</label>
    <label for="f-girl">Use a girl's saved photo</label>
    <select id="f-girl"><option value="">— none —</option></select>
    <p class="hint" id="girl-hint"></p>
    <label for="f-upload">…or upload a photo</label>
    <input type="file" id="f-upload" accept="image/*">
  </div>
  <button class="btn" id="btn-generate" type="button">✨ Generate image</button>
  <p class="status" id="status"></p>
  <div id="result"></div>
  <div class="card">
    <label>Recent generations</label>
    <div class="strip" id="recent"><p class="hint">Nothing yet.</p></div>
  </div>
</section>
</main>

<script>
const DATA = __STUDIO_DATA__;
const $ = id => document.getElementById(id);
const qs = new URLSearchParams(location.search);
let adminTok = qs.get('admin_token') || sessionStorage.getItem('studio_admin') || '';
if (qs.get('admin_token')) {
  sessionStorage.setItem('studio_admin', adminTok);
  history.replaceState(null, '', '/studio');
}
// The app's own session door: app.js getToken() reads localStorage
// 'whispr_token' and sends it as 'Authorization: Bearer <token>'.
const sessionTok = localStorage.getItem('whispr_token') || '';
function authHeaders() {
  return adminTok ? {'X-Admin-Token': adminTok}
                  : {'Authorization': 'Bearer ' + sessionTok};
}
function fields() {
  return {mode: $('f-mode').value, subject: $('f-subject').value,
    background: $('f-background').value, camera_angle: $('f-angle').value,
    framing: $('f-framing').value, style: $('f-style').value,
    palette: $('f-palette').value, details: $('f-details').value};
}
function sentence(t) { t = (t || '').trim(); if (!t) return '';
  return /[.!?]$/.test(t) ? t : t + '.'; }
function assemble(f) {
  const subject = (f.subject || '').trim(); if (!subject) return '';
  const parts = [sentence(subject)];
  if ((f.background || '').trim()) parts.push(sentence(f.background));
  const comp = [];
  const a = DATA.angles[f.camera_angle] || '', fr = DATA.framings[f.framing] || '';
  if (a) comp.push(a); if (fr) comp.push(fr);
  if (comp.length) parts.push('Composition: ' + comp.join(', ') + '.');
  const st = (DATA.styles[f.style] || [])[1]; if (st) parts.push(st);
  if ((f.palette || '').trim()) parts.push(sentence('Color palette: ' + f.palette.trim()));
  if ((f.details || '').trim()) parts.push(sentence(f.details));
  let p = parts.join(' ');
  if (f.mode === 'edit') p = 'Edit the reference image: ' + p;
  return p;
}
let promptTouched = false;
function refreshPrompt() { if (!promptTouched) $('f-prompt').value = assemble(fields()); syncRef(); }
function syncRef() { $('ref-card').hidden = fields().mode !== 'edit'; }
['f-mode','f-subject','f-background','f-angle','f-framing','f-style','f-palette','f-details']
  .forEach(id => $(id).addEventListener('input', refreshPrompt));
$('f-prompt').addEventListener('input', () => { promptTouched = true; });
function fillSelect(sel, obj, isStyle) {
  sel.innerHTML = '';
  Object.keys(obj).forEach(k => {
    const o = document.createElement('option'); o.value = k;
    o.textContent = isStyle ? obj[k][0] : (k === 'default' ? 'Default' :
      k.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase()));
    sel.appendChild(o);
  });
}
fillSelect($('f-angle'), DATA.angles, false);
fillSelect($('f-framing'), DATA.framings, false);
fillSelect($('f-style'), DATA.styles, true);
Object.keys(DATA.templates).forEach(k => {
  const chip = document.createElement('span'); chip.className = 'chip';
  chip.textContent = DATA.templates[k][0];
  chip.onclick = () => {
    const f = DATA.templates[k][1];
    $('f-mode').value = f.mode; $('f-subject').value = f.subject;
    $('f-background').value = f.background; $('f-angle').value = f.camera_angle;
    $('f-framing').value = f.framing; $('f-style').value = f.style;
    $('f-palette').value = f.palette; $('f-details').value = f.details;
    promptTouched = false; refreshPrompt();
  };
  $('templates').appendChild(chip);
});
$('btn-copy').onclick = async () => {
  try { await navigator.clipboard.writeText($('f-prompt').value); setStatus('Prompt copied.', 'ok'); }
  catch (e) { $('f-prompt').select(); document.execCommand('copy'); setStatus('Prompt copied.', 'ok'); }
};
$('btn-clear').onclick = () => {
  ['f-subject','f-background','f-palette','f-details','f-prompt'].forEach(id => $(id).value = '');
  promptTouched = false; refreshPrompt();
};
function setStatus(msg, cls) { const s = $('status'); s.textContent = msg; s.className = 'status ' + (cls || ''); }
function showTab(which) {
  $('pane-builder').hidden = which !== 'builder';
  $('pane-generate').hidden = which !== 'generate';
  $('tab-builder').classList.toggle('on', which === 'builder');
  $('tab-generate').classList.toggle('on', which === 'generate');
  if (which === 'generate') loadRecent();
}
$('tab-builder').onclick = () => showTab('builder');
$('tab-generate').onclick = () => showTab('generate');
async function loadGirls() {
  try {
    const r = await fetch('/api/v1/studio/girls', {headers: authHeaders()});
    if (!r.ok) return;
    const data = await r.json();
    const sel = $('f-girl');
    data.girls.forEach(g => {
      const o = document.createElement('option'); o.value = g.id;
      o.textContent = g.name + (g.has_reference ? '' : ' — no saved photo, upload instead');
      o.disabled = !g.has_reference; sel.appendChild(o);
    });
  } catch (e) { /* picker stays empty; upload still works */ }
}
let uploadB64 = '';
$('f-upload').addEventListener('change', () => {
  const file = $('f-upload').files[0]; if (!file) { uploadB64 = ''; return; }
  const rd = new FileReader();
  rd.onload = () => { uploadB64 = rd.result; };
  rd.readAsDataURL(file);
});
async function loadRecent() {
  try {
    const r = await fetch('/api/v1/studio/recent', {headers: authHeaders()});
    if (!r.ok) return;
    const data = await r.json();
    const strip = $('recent');
    if (!data.recent.length) { strip.innerHTML = '<p class="hint">Nothing yet.</p>'; return; }
    strip.innerHTML = '';
    data.recent.forEach(item => {
      const fig = document.createElement('figure');
      const img = document.createElement('img'); img.src = item.url; img.alt = item.prompt || '';
      const cap = document.createElement('figcaption'); cap.textContent = item.prompt || '';
      fig.appendChild(img); fig.appendChild(cap); strip.appendChild(fig);
    });
  } catch (e) { /* strip keeps its placeholder */ }
}
$('btn-generate').onclick = async () => {
  const f = fields();
  const body = {prompt: $('f-prompt').value, fields: f, mode: f.mode};
  if (f.mode === 'edit') {
    if (uploadB64) body.image_b64 = uploadB64;
    else if ($('f-girl').value) body.girl_id = $('f-girl').value;
  }
  if (!body.prompt.trim()) { setStatus('Write a subject first — the prompt is empty.', 'err'); showTab('builder'); return; }
  const btn = $('btn-generate'); btn.disabled = true;
  setStatus('Generating…');
  try {
    const r = await fetch('/api/v1/studio/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', ...authHeaders()},
      body: JSON.stringify(body)});
    const data = await r.json().catch(() => ({}));
    if (r.ok && data.url) {
      $('result').innerHTML = '';
      const img = document.createElement('img'); img.src = data.url;
      $('result').appendChild(img);
      setStatus('Done.', 'ok'); loadRecent();
    } else if (r.status === 422 && data.detail && data.detail.error === 'model_refused') {
      setStatus('The model refused this one (' + data.detail.reason + ')' +
        (data.detail.message ? ': ' + data.detail.message : '') +
        ' — that refusal is the model\'s own, not a filter here.', 'err');
    } else if (r.status === 502) {
      setStatus('The generation pool could not serve that call. Nothing was generated.', 'err');
    } else {
      const d = typeof data.detail === 'string' ? data.detail : 'something went wrong';
      setStatus('Not generated (' + d + ').', 'err');
    }
  } catch (e) {
    setStatus('Not generated — the request never landed.', 'err');
  } finally { btn.disabled = false; }
};
async function gateOpen() {
  // The data endpoints are the real gate — probe the cheapest one with
  // whatever credential this browser holds. Only a valid OWNER session
  // (or the admin token) answers 200; anonymous gets 401 and a
  // non-owner session gets 403, and both land on the notice below.
  if (!adminTok && !sessionTok) return false;
  try {
    const r = await fetch('/api/v1/studio/recent', {headers: authHeaders()});
    return r.ok;
  } catch (e) { return false; }
}
(async () => {
  if (await gateOpen()) {
    $('gate').hidden = true;
    $('studio-ui').hidden = false;
    refreshPrompt();
    loadGirls();
  } else {
    $('gate-msg').textContent = 'Owner only.';
  }
})();
</script>
</body>
</html>
"""


def _page_html() -> str:
    data = {
        "angles": CAMERA_ANGLES,
        "framings": FRAMINGS,
        "styles": {k: [v[0], v[1]] for k, v in STYLE_PRESETS.items()},
        "templates": {k: [v[0], v[1]] for k, v in TEMPLATES.items()},
    }
    return _PAGE.replace("__STUDIO_DATA__", json.dumps(data))


def register(app) -> None:
    """Attach the studio page + its three API routes to the app."""

    @app.get("/studio", response_class=HTMLResponse, include_in_schema=False)
    def studio_page(request: Request):
        # The shell is public on purpose: it holds no data, no roster
        # and no secrets — just the UI. A plain navigation can present
        # neither the app's Bearer session nor the admin token, so the
        # gate lives in the page's own calls to the endpoints below
        # (each one re-checks require_studio_access server-side); the
        # page probes one on load and shows its "Owner only." notice
        # to anyone whose token does not open the gate.
        return HTMLResponse(_page_html(),
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/studio/girls")
    def studio_girls(request: Request):
        require_studio_access(request)
        return girls_payload()

    @app.get("/api/v1/studio/recent")
    def studio_recent(request: Request):
        require_studio_access(request)
        return {"recent": list_recent()}

    @app.post("/api/v1/studio/generate")
    def studio_generate(body: StudioGenerateIn, request: Request):
        require_studio_access(request)
        return generate(body.model_dump())
