"""Public permalink page for a single Her Day post (2026-10-17).

Server-rendered so link previews (X, Reddit, Telegram, iMessage) get
real Open Graph tags — the photo, her name, the caption — and the page
itself funnels to her chat with the standard free-messages offer.
"""
from __future__ import annotations

import html
from typing import Any

APP_BASE = "https://lockeddoor.ai"


def _abs(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if u.startswith("data:"):
        return ""
    return APP_BASE + "/" + u.lstrip("/")


def render_post_page(post: dict[str, Any] | None, girl: dict[str, Any] | None,
                     slug: str, status_line: str = "") -> str:
    if not post or not girl:
        return _page(
            title="Whispr AI — Meet Your New Obsession",
            desc="Your private connection, always a message away. 50 free messages, no card required.",
            image="",
            body=_not_found_body(),
        )
    name = html.escape(girl.get("name") or post.get("girl_name") or "Her")
    girl_id = html.escape(post.get("girl_id") or girl.get("id") or "chloe")
    caption = (post.get("caption") or "").strip()
    cap_esc = html.escape(caption)
    media = _abs(post.get("media_url") or "")
    voice = _abs(post.get("voice_url") or "")
    avatar = _abs(girl.get("avatar_url") or "")
    cta = f"{APP_BASE}/?character={girl_id}"
    media_html = ""
    if post.get("post_type") == "video" and media:
        media_html = f'<video class="media" src="{html.escape(media)}" controls playsinline></video>'
    elif media:
        media_html = f'<img class="media" src="{html.escape(media)}" alt="{name} on Whispr">'
    elif voice:
        media_html = f'<audio class="media-audio" src="{html.escape(voice)}" controls></audio>'
    avatar_html = (f'<img class="avatar" src="{html.escape(avatar)}" alt="{name}">'
                   if avatar else f'<div class="avatar avatar-fallback">{name[:1]}</div>')
    body = f"""
      <div class="card">
        <div class="who">{avatar_html}<div><div class="name">{name}</div>
        <div class="sub">Whispr AI · her day{(' · 📍 ' + html.escape(status_line)) if status_line else ''}</div></div></div>
        {media_html}
        <p class="caption">{cap_esc}</p>
        <a class="cta" href="{cta}">Chat with {name} — free</a>
        <p class="fine">50 free messages · no card required</p>
      </div>"""
    return _page(
        title=f"{name} on Whispr 💜",
        desc=caption[:180] or f"See what {name} is up to today on Whispr.",
        image=media,
        body=body,
    )


def _not_found_body() -> str:
    return f"""
      <div class="card">
        <p class="caption">This moment has passed — her day moves fast. 💜</p>
        <a class="cta" href="{APP_BASE}/">Meet the girls</a>
        <p class="fine">50 free messages · no card required</p>
      </div>"""


def _page(title: str, desc: str, image: str, body: str) -> str:
    t = html.escape(title)
    d = html.escape(desc)
    img_tag = (f'<meta property="og:image" content="{html.escape(image)}">'
               f'<meta name="twitter:card" content="summary_large_image">'
               if image else '<meta name="twitter:card" content="summary">')
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t}</title>
<meta name="description" content="{d}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Whispr AI">
<meta property="og:title" content="{t}">
<meta property="og:description" content="{d}">
{img_tag}
<meta name="twitter:title" content="{t}">
<meta name="twitter:description" content="{d}">
<style>
body{{margin:0;background:#0b0b12;color:#f4f1f8;font-family:-apple-system,'Segoe UI',Roboto,sans-serif}}
.wrap{{max-width:520px;margin:0 auto;padding:28px 16px 48px}}
.brand{{text-align:center;font-size:30px;font-weight:800;background:linear-gradient(90deg,#e05aa0,#8a5cf6);-webkit-background-clip:text;background-clip:text;color:transparent}}
.tag{{text-align:center;color:#a49db3;margin:4px 0 22px}}
.card{{background:#15151f;border:1px solid #262633;border-radius:18px;padding:18px}}
.who{{display:flex;gap:12px;align-items:center;margin-bottom:12px}}
.avatar{{width:46px;height:46px;border-radius:50%;object-fit:cover}}
.avatar-fallback{{display:flex;align-items:center;justify-content:center;background:#8a5cf6;font-weight:700;font-size:20px}}
.name{{font-weight:700;font-size:17px}}
.sub{{color:#a49db3;font-size:12px}}
.media{{width:100%;border-radius:12px;display:block}}
.media-audio{{width:100%}}
.caption{{font-size:16px;line-height:1.5;white-space:pre-wrap}}
.cta{{display:block;text-align:center;background:linear-gradient(90deg,#e05aa0,#8a5cf6);color:#fff;text-decoration:none;font-weight:700;padding:13px;border-radius:12px;margin-top:14px}}
.fine{{text-align:center;color:#a49db3;font-size:12px;margin:10px 0 0}}
</style></head><body><div class="wrap">
<div class="brand">Whispr AI</div>
<p class="tag">Your private connection, always a message away.</p>
{body}
</div></body></html>"""
