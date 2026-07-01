#!/usr/bin/env python3
"""
Blogger -> LinkedIn + WhatsApp Channel auto-share agent.

Detects new posts on a Blogger blog and shares each new post to:
  * LinkedIn Page/Profile  -> article share with title + excerpt + link
  * WhatsApp Channel       -> text + link via WhatsApp Business Cloud API

State tracked per platform in state.json so no duplicate posts.

Required environment variables (GitHub Secrets):
    BLOGGER_API_KEY   - Google API key with Blogger API enabled
    BLOG_ID           - Blogger blog numeric ID

    # LinkedIn
    LI_ACCESS_TOKEN   - LinkedIn OAuth2 access token (60-day, refreshable)
    LI_AUTHOR_URN     - "urn:li:person:XXXX" or "urn:li:organization:XXXX"

    # WhatsApp Channel (Meta Business Cloud API)
    WA_PHONE_NUMBER_ID  - WhatsApp Business phone number ID
    WA_ACCESS_TOKEN     - Meta permanent access token
    WA_CHANNEL_ID       - WhatsApp channel/newsletter ID

Optional:
    MAX_CATCHUP   - Max backlog posts per run (default 1)
    DRY_RUN       - "true" => log only, no actual posts
    ENABLE_LI     - "true"/"false" (default true)
    ENABLE_WA     - "true"/"false" (default true)
    ENABLE_FB     - Legacy Facebook (default false — use only if token works)
    ENABLE_TG     - Legacy Telegram  (default false)
"""

import json
import os
import re
import sys
import html
from pathlib import Path
from urllib import request, parse, error

GRAPH_VERSION = "v21.0"
STATE_FILE = Path(__file__).parent / "state.json"


# ── Helpers ───────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[share-agent] {msg}", flush=True)


def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        log(f"ERROR: missing required env var {name}")
        sys.exit(1)
    return val


def env_bool(name, default=True):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def http_get_json(url):
    req = request.Request(url, headers={"User-Agent": "share-agent/2.0"})
    with request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post(url, data, timeout=60, headers=None):
    encoded = parse.urlencode(data).encode("utf-8")
    h = {"User-Agent": "share-agent/2.0", "Content-Type": "application/x-www-form-urlencoded"}
    if headers:
        h.update(headers)
    req = request.Request(url, data=encoded, method="POST", headers=h)
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_json(url, payload, token, timeout=60):
    """POST with JSON body and Bearer token — used by LinkedIn."""
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            "User-Agent":    "share-agent/2.0",
            "X-Restli-Protocol-Version": "2.0.0",
        },
    )
    with request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {"status": resp.status}


def load_state():
    if STATE_FILE.exists():
        try:
            s = json.loads(STATE_FILE.read_text())
            for k in ("facebook", "telegram", "linkedin", "whatsapp"):
                s.setdefault(k, [])
            s.setdefault("seeded", False)
            return s
        except json.JSONDecodeError:
            log("state.json corrupt — starting fresh")
    return {"facebook": [], "telegram": [], "linkedin": [], "whatsapp": [], "seeded": False}


def save_state(state):
    for k in ("facebook", "telegram", "linkedin", "whatsapp"):
        state[k] = state[k][-500:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Blogger ───────────────────────────────────────────────────────────────────
def fetch_recent_posts(blog_id, api_key, count=10):
    url = (
        f"https://www.googleapis.com/blogger/v3/blogs/{blog_id}/posts"
        f"?key={api_key}&maxResults={count}&fetchImages=true&status=LIVE"
        f"&orderBy=published"
    )
    return http_get_json(url).get("items", [])


PLACEHOLDER_HOSTS = (
    "picsum.photos", "via.placeholder.com", "placehold.co",
    "placekitten.com", "dummyimage.com", "1x1.gif", "data:image",
)
PREFERRED_HOSTS = (
    "pollinations.ai", "blogger.googleusercontent.com",
    "bp.blogspot.com", "lh3.googleusercontent.com",
    "catbox.moe", "files.catbox.moe",
)


def extract_image(post):
    content = post.get("content", "") or ""
    for host_list in (PREFERRED_HOSTS, None):
        for attr in ("data-src", "data-lazy-src", "data-original", "src"):
            for m in re.finditer(
                r'<img[^>]+' + attr + r'=["\']([^"\']+)["\']', content, re.IGNORECASE
            ):
                u = m.group(1)
                bad = any(h in u.lower() for h in PLACEHOLDER_HOSTS)
                if bad:
                    continue
                if host_list is None or any(h in u.lower() for h in host_list):
                    return u
    images = post.get("images")
    if images and isinstance(images, list):
        u = images[0].get("url", "")
        if u and not any(h in u.lower() for h in PLACEHOLDER_HOSTS):
            return u
    return None


def plain_excerpt(post, limit=300):
    content = post.get("content", "") or ""
    content = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ",
                     content, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", content)
    text = html.unescape(text)
    text = re.sub(r'\{[^}]*"@context"[^}]*\}', " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


# ── LinkedIn ──────────────────────────────────────────────────────────────────
def post_to_linkedin(author_urn, token, title, link, excerpt, image_url, dry_run=False):
    """
    Share an article on LinkedIn using the UGC Posts API.
    Uses ARTICLE media category with og-style link attachment.
    """
    text = f"{title}\n\n{excerpt}\n\n🔬 पूरा पढ़ें: {link}"
    if len(text) > 3000:
        text = text[:2990] + "…"

    payload = {
        "author": author_urn,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": text},
                "shareMediaCategory": "ARTICLE",
                "media": [
                    {
                        "status": "READY",
                        "originalUrl": link,
                        "title": {"text": title[:200]},
                        "description": {"text": excerpt[:400]},
                    }
                ],
            }
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
        },
    }

    if dry_run:
        log(f"   [DRY] LI post: {title[:60]}")
        return {"dry_run": True}

    try:
        resp = http_post_json(
            "https://api.linkedin.com/v2/ugcPosts",
            payload, token, timeout=30,
        )
        return resp
    except error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        raise Exception(f"HTTP {e.code}: {body[:200]}")


# ── WhatsApp Channel ──────────────────────────────────────────────────────────
def post_to_whatsapp(phone_number_id, wa_token, channel_id, title, link, excerpt, dry_run=False):
    """
    Post to a WhatsApp Channel via Meta Business Cloud API.
    Uses sendMessage endpoint with newsletter/channel type.
    """
    # Build message text (WhatsApp supports basic formatting)
    msg = f"*{title}*\n\n{excerpt}\n\n🔗 {link}"
    if len(msg) > 4096:
        msg = msg[:4080] + "…"

    if dry_run:
        log(f"   [DRY] WA post: {title[:60]}")
        return {"dry_run": True}

    # WhatsApp Business Cloud API — send to channel
    url = (
        f"https://graph.facebook.com/{GRAPH_VERSION}/"
        f"{phone_number_id}/messages"
    )
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": channel_id,
        "type": "text",
        "text": json.dumps({"preview_url": True, "body": msg}),
        "access_token": wa_token,
    }
    return http_post(url, payload, timeout=30)


# ── Legacy Facebook (optional) ────────────────────────────────────────────────
def post_to_facebook(page_id, token, title, link, image_url, dry_run=False):
    caption = f"{title}\n\n{link}"
    if dry_run:
        log(f"   [DRY] FB: {caption[:60]}")
        return {"dry_run": True}
    if image_url:
        endpoint = f"https://graph.facebook.com/{GRAPH_VERSION}/{page_id}/photos"
        payload = {"url": image_url, "caption": caption, "access_token": token}
    else:
        endpoint = f"https://graph.facebook.com/{GRAPH_VERSION}/{page_id}/feed"
        payload = {"message": title, "link": link, "access_token": token}
    return http_post(endpoint, payload)


# ── Legacy Telegram (optional) ────────────────────────────────────────────────
def post_to_telegram(bot_token, chat_id, title, link, excerpt, image_url, dry_run=False):
    safe_title   = html.escape(title)
    safe_excerpt = html.escape(excerpt)
    safe_link    = html.escape(link, quote=True)
    caption = (
        f"<b>{safe_title}</b>\n\n{safe_excerpt}\n\n"
        f"<a href=\"{safe_link}\">पूरा पढ़ें »</a>"
    )
    if len(caption) > 1024:
        caption = caption[:1000].rsplit(" ", 1)[0] + "…"
    if dry_run:
        log(f"   [DRY] TG: {title[:60]}")
        return {"dry_run": True}
    base = f"https://api.telegram.org/bot{bot_token}"
    text_payload = {
        "chat_id": chat_id, "text": caption,
        "parse_mode": "HTML", "disable_web_page_preview": "false",
    }
    if image_url:
        photo_payload = {
            "chat_id": chat_id, "photo": image_url,
            "caption": caption, "parse_mode": "HTML",
        }
        try:
            return http_post(f"{base}/sendPhoto", photo_payload, timeout=120)
        except Exception as e:
            log(f"   TG photo failed ({e}); falling back to text.")
            return http_post(f"{base}/sendMessage", text_payload, timeout=60)
    return http_post(f"{base}/sendMessage", text_payload, timeout=60)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    blog_id    = env("BLOG_ID", required=True)
    api_key    = env("BLOGGER_API_KEY", required=True)
    max_catchup = int(env("MAX_CATCHUP", "1"))
    dry_run    = env_bool("DRY_RUN", False)

    # Platform toggles
    enable_li  = env_bool("ENABLE_LI", True)
    enable_wa  = env_bool("ENABLE_WA", True)
    enable_fb  = env_bool("ENABLE_FB", False)   # disabled by default
    enable_tg  = env_bool("ENABLE_TG", False)   # disabled by default

    # LinkedIn
    li_token      = env("LI_ACCESS_TOKEN")
    li_author_urn = env("LI_AUTHOR_URN")

    # WhatsApp
    wa_phone_id  = env("WA_PHONE_NUMBER_ID")
    wa_token     = env("WA_ACCESS_TOKEN")
    wa_channel   = env("WA_CHANNEL_ID")

    # Legacy
    fb_page_id = env("FB_PAGE_ID")
    fb_token   = env("FB_PAGE_TOKEN")
    tg_bot     = env("TG_BOT_TOKEN")
    tg_chat    = env("TG_CHAT_ID")

    # Disable if credentials missing
    if enable_li and not (li_token and li_author_urn):
        log("LinkedIn credentials missing — disabling LI.")
        enable_li = False
    if enable_wa and not (wa_phone_id and wa_token and wa_channel):
        log("WhatsApp credentials missing — disabling WA.")
        enable_wa = False
    if enable_fb and not (fb_page_id and fb_token):
        log("Facebook credentials missing — disabling FB.")
        enable_fb = False
    if enable_tg and not (tg_bot and tg_chat):
        log("Telegram credentials missing — disabling TG.")
        enable_tg = False

    state   = load_state()
    li_done = set(state["linkedin"])
    wa_done = set(state["whatsapp"])
    fb_done = set(state["facebook"])
    tg_done = set(state["telegram"])

    log("Fetching recent posts from Blogger...")
    try:
        posts = fetch_recent_posts(blog_id, api_key, count=10)
    except error.HTTPError as e:
        log(f"Blogger API error {e.code}: {e.read().decode('utf-8','ignore')}")
        sys.exit(1)

    if not posts:
        log("No posts found.")
        return

    if not state["seeded"]:
        log("First run — seeding history.")
        for p in posts[max_catchup:]:
            for done_set in (li_done, wa_done, fb_done, tg_done):
                done_set.add(p["id"])
        state["seeded"] = True

    ordered = list(reversed(posts))  # oldest-first
    li_count = wa_count = fb_count = tg_count = 0

    for post in ordered:
        pid     = post["id"]
        title   = post.get("title", "(untitled)")
        link    = post.get("url", "")
        image   = extract_image(post)
        excerpt = plain_excerpt(post)

        # ── LinkedIn ──────────────────────────────────────────────────────────
        if enable_li and pid not in li_done:
            log(f"LI  sharing: {title}")
            try:
                resp = post_to_linkedin(
                    li_author_urn, li_token, title, link, excerpt, image,
                    dry_run=dry_run,
                )
                log(f"   LI -> {str(resp)[:120]}")
                li_done.add(pid)
                li_count += 1
            except Exception as e:
                log(f"   !! LI error: {e}")

        # ── WhatsApp Channel ──────────────────────────────────────────────────
        if enable_wa and pid not in wa_done:
            log(f"WA  sharing: {title}")
            try:
                resp = post_to_whatsapp(
                    wa_phone_id, wa_token, wa_channel,
                    title, link, excerpt, dry_run=dry_run,
                )
                log(f"   WA -> {str(resp)[:120]}")
                wa_done.add(pid)
                wa_count += 1
            except Exception as e:
                log(f"   !! WA error: {e}")

        # ── Legacy Facebook ───────────────────────────────────────────────────
        if enable_fb and pid not in fb_done:
            log(f"FB  sharing: {title}")
            try:
                resp = post_to_facebook(
                    fb_page_id, fb_token, title, link, image, dry_run=dry_run
                )
                log(f"   FB -> {str(resp)[:120]}")
                fb_done.add(pid)
                fb_count += 1
            except error.HTTPError as e:
                log(f"   !! FB error {e.code}: {e.read().decode('utf-8','ignore')[:100]}")
            except Exception as e:
                log(f"   !! FB unexpected: {e}")

        # ── Legacy Telegram ───────────────────────────────────────────────────
        if enable_tg and pid not in tg_done:
            log(f"TG  sharing: {title}")
            try:
                resp = post_to_telegram(
                    tg_bot, tg_chat, title, link, excerpt, image, dry_run=dry_run
                )
                ok = resp.get("ok", True) if isinstance(resp, dict) else True
                log(f"   TG -> {str(resp)[:120]}")
                if ok:
                    tg_done.add(pid)
                    tg_count += 1
                else:
                    log("   !! TG ok=false — will retry next run.")
            except error.HTTPError as e:
                log(f"   !! TG error {e.code}: {e.read().decode('utf-8','ignore')[:100]}")
            except Exception as e:
                log(f"   !! TG unexpected: {e}")

    state["linkedin"]  = list(li_done)
    state["whatsapp"]  = list(wa_done)
    state["facebook"]  = list(fb_done)
    state["telegram"]  = list(tg_done)
    save_state(state)
    log(
        f"Done. LinkedIn posted {li_count}, WhatsApp posted {wa_count}, "
        f"Facebook posted {fb_count}, Telegram posted {tg_count}."
    )


if __name__ == "__main__":
    main()
