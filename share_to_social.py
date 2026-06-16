#!/usr/bin/env python3
"""
Blogger -> Facebook Page + Telegram Channel auto-share agent.

Detects new posts on a Blogger blog (via the Blogger API) and shares each new
post to:
  * a Facebook Page      -> photo post: title + featured image + link
  * a Telegram Channel   -> photo message: image + title + excerpt + clickable
                            link (Telegram captions allow real hyperlinks)

State is tracked PER PLATFORM in `state.json`, so if one platform fails but the
other succeeds, only the failed one is retried next run (no duplicate posts).

The GitHub Actions workflow commits the updated state.json back to the repo
after each run so nothing is ever posted twice.

Required environment variables (set as GitHub repository secrets):
    BLOGGER_API_KEY   - Google API key with the Blogger API enabled
    BLOG_ID           - Your Blogger blog's numeric ID

    # Facebook
    FB_PAGE_ID        - Your Facebook Page's numeric ID
    FB_PAGE_TOKEN     - Long-lived (non-expiring) Page access token

    # Telegram
    TG_BOT_TOKEN      - Bot token from @BotFather (looks like 123456:ABC-...)
    TG_CHAT_ID        - Channel username like "@vigyankiduniya" or numeric -100... id
                        (the bot must be an ADMIN of the channel)

Optional:
    MAX_CATCHUP   - Max backlog posts to share in one run (default 1).
    DRY_RUN       - "true" => log actions without actually posting.
    ENABLE_FB     - "true"/"false" (default true)
    ENABLE_TG     - "true"/"false" (default true)
"""

import json
import os
import re
import sys
import html
from pathlib import Path
from urllib import request, parse, error

GRAPH_VERSION = "v25.0"
STATE_FILE = Path(__file__).parent / "state.json"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def log(msg):
    print(f"[fb-tg-agent] {msg}", flush=True)


def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        log(f"ERROR: missing required environment variable {name}")
        sys.exit(1)
    return val


def env_bool(name, default=True):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def http_get_json(url):
    req = request.Request(url, headers={"User-Agent": "blogger-fb-tg-agent/1.0"})
    with request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post(url, data, timeout=60):
    encoded = parse.urlencode(data).encode("utf-8")
    req = request.Request(url, data=encoded, method="POST",
                          headers={"User-Agent": "blogger-fb-tg-agent/1.0"})
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_state():
    if STATE_FILE.exists():
        try:
            s = json.loads(STATE_FILE.read_text())
            s.setdefault("facebook", [])
            s.setdefault("telegram", [])
            s.setdefault("seeded", False)
            return s
        except json.JSONDecodeError:
            log("state.json was corrupt; starting fresh")
    return {"facebook": [], "telegram": [], "seeded": False}


def save_state(state):
    for k in ("facebook", "telegram"):
        state[k] = state[k][-500:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------- #
# Blogger
# --------------------------------------------------------------------------- #
def fetch_recent_posts(blog_id, api_key, count=10):
    url = (
        f"https://www.googleapis.com/blogger/v3/blogs/{blog_id}/posts"
        f"?key={api_key}&maxResults={count}&fetchImages=true&status=LIVE"
        f"&orderBy=published"
    )
    return http_get_json(url).get("items", [])


# Hosts that serve random/placeholder images, not real post images.
PLACEHOLDER_IMAGE_HOSTS = (
    "picsum.photos",
    "via.placeholder.com",
    "placehold.co",
    "placekitten.com",
    "dummyimage.com",
    "1x1.gif",
    "data:image",            # inline base64 lazy-load placeholders
)


def _is_placeholder(url):
    low = url.lower()
    return any(h in low for h in PLACEHOLDER_IMAGE_HOSTS)


# Hosts that serve REAL post images we should actively prefer.
PREFERRED_IMAGE_HOSTS = (
    "pollinations.ai",
    "blogger.googleusercontent.com",
    "bp.blogspot.com",
    "lh3.googleusercontent.com",
)


def _find_in_content_by_host(content, hosts):
    """Return the first image URL in the content served from one of `hosts`."""
    for m in re.finditer(r'<img[^>]+>', content, re.IGNORECASE):
        tag = m.group(0)
        # check all url-bearing attributes on this <img>
        for attr in ("data-src", "data-lazy-src", "data-original", "src"):
            am = re.search(attr + r'=["\']([^"\']+)["\']', tag, re.IGNORECASE)
            if am:
                url = am.group(1)
                low = url.lower()
                if any(h in low for h in hosts) and not _is_placeholder(url):
                    return url
    return None


def _og_image_from_page(post_url):
    """
    Fetch the post's page HTML and read its <meta property="og:image">.
    Best-effort only: many blogs sit behind bot protection (Cloudflare etc.)
    that returns 403 to server requests, so this often fails -- callers must
    treat None as normal and fall back.
    """
    if not post_url:
        return None
    try:
        req = request.Request(
            post_url,
            headers={"User-Agent": "Mozilla/5.0 (blogger-fb-tg-agent)"},
        )
        with request.urlopen(req, timeout=20) as resp:
            head = resp.read(120_000).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return None

    for pattern in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    ):
        m = re.search(pattern, head, re.IGNORECASE)
        if m and not _is_placeholder(m.group(1)):
            return m.group(1)
    return None


def extract_image(post):
    content = post.get("content", "") or ""

    # 1. Strongly prefer a known real-image host in the post body
    #    (Pollinations AI image, Blogger/Blogspot uploads, etc.).
    url = _find_in_content_by_host(content, PREFERRED_IMAGE_HOSTS)
    if url:
        return url

    # 2. Blogger API's own image field, unless it's a placeholder.
    images = post.get("images")
    if images and isinstance(images, list) and images[0].get("url"):
        u = images[0]["url"]
        if not _is_placeholder(u):
            return u

    # 3. Any other real <img> in the body (lazy-load attrs first), skipping
    #    placeholders.
    for attr in ("data-src", "data-lazy-src", "data-original", "src"):
        for m in re.finditer(
            r'<img[^>]+' + attr + r'=["\']([^"\']+)["\']',
            content, re.IGNORECASE,
        ):
            u = m.group(1)
            if u and not _is_placeholder(u):
                return u

    # 4. Last resort: page og:image (often blocked by bot protection).
    return _og_image_from_page(post.get("url"))


def plain_excerpt(post, limit=300):
    content = post.get("content", "") or ""
    # Strip <script>/<style> blocks first so embedded JSON-LD schema metadata
    # and CSS don't leak into the excerpt.
    content = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ",
                     content, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", content)        # strip remaining tags
    text = html.unescape(text)
    # Drop any leftover JSON-LD-ish noise (lines that are mostly punctuation/braces).
    text = re.sub(r'\{[^}]*"@context"[^}]*\}', " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "\u2026"
    return text


# --------------------------------------------------------------------------- #
# Facebook
# --------------------------------------------------------------------------- #
def post_to_facebook(page_id, token, title, link, image_url, dry_run=False):
    caption = f"{title}\n\n{link}"
    if dry_run:
        log(f"   [DRY] FB caption={caption!r} image={image_url}")
        return {"dry_run": True}
    if image_url:
        endpoint = f"https://graph.facebook.com/{GRAPH_VERSION}/{page_id}/photos"
        payload = {"url": image_url, "caption": caption, "access_token": token}
    else:
        endpoint = f"https://graph.facebook.com/{GRAPH_VERSION}/{page_id}/feed"
        payload = {"message": title, "link": link, "access_token": token}
    return http_post(endpoint, payload)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def _warm_up_image(image_url):
    """
    Request the image once ourselves so a lazily-generated image (e.g. a
    Pollinations URL behind Google's proxy) is rendered/cached before we ask
    Telegram to fetch it. Best-effort; ignores all errors.
    """
    if not image_url:
        return
    try:
        req = request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
        with request.urlopen(req, timeout=45) as r:
            r.read(2048)  # touch the first bytes; that's enough to trigger gen
    except Exception:  # noqa: BLE001
        pass


def post_to_telegram(bot_token, chat_id, title, link, excerpt, image_url,
                     dry_run=False):
    """
    Telegram caption supports HTML, so we make the title bold and the link a
    real clickable "Read more" anchor. Caption hard limit is 1024 chars.

    If sending the photo fails (e.g. Telegram times out fetching a slow image
    URL), fall back to a plain text message with a link preview so the post is
    never silently lost.
    """
    safe_title = html.escape(title)
    safe_excerpt = html.escape(excerpt)
    safe_link = html.escape(link, quote=True)
    caption = (
        f"<b>{safe_title}</b>\n\n"
        f"{safe_excerpt}\n\n"
        f"<a href=\"{safe_link}\">\u092a\u0942\u0930\u093e \u092a\u0922\u093c\u0947\u0902 \u00bb</a>"  # "पूरा पढ़ें »"
    )
    if len(caption) > 1024:
        caption = caption[:1000].rsplit(" ", 1)[0] + "\u2026"

    if dry_run:
        log(f"   [DRY] TG chat={chat_id} caption={caption!r} image={image_url}")
        return {"dry_run": True}

    send_msg_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    text_payload = {
        "chat_id": chat_id,
        "text": caption,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }

    if image_url:
        _warm_up_image(image_url)  # pre-render slow images before Telegram fetches
        photo_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        photo_payload = {
            "chat_id": chat_id,
            "photo": image_url,
            "caption": caption,
            "parse_mode": "HTML",
        }
        try:
            # Generous timeout: Telegram must fetch the image server-side.
            return http_post(photo_url, photo_payload, timeout=120)
        except Exception as e:  # noqa: BLE001
            log(f"   TG photo failed ({e}); falling back to text+link message.")
            return http_post(send_msg_url, text_payload, timeout=60)

    return http_post(send_msg_url, text_payload, timeout=60)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    blog_id = env("BLOG_ID", required=True)
    api_key = env("BLOGGER_API_KEY", required=True)
    max_catchup = int(env("MAX_CATCHUP", "1"))
    dry_run = env_bool("DRY_RUN", False)

    enable_fb = env_bool("ENABLE_FB", True)
    enable_tg = env_bool("ENABLE_TG", True)

    fb_page_id = env("FB_PAGE_ID")
    fb_token = env("FB_PAGE_TOKEN")
    tg_bot_token = env("TG_BOT_TOKEN")
    tg_chat_id = env("TG_CHAT_ID")

    if enable_fb and not (fb_page_id and fb_token):
        log("Facebook enabled but FB_PAGE_ID/FB_PAGE_TOKEN missing; disabling FB.")
        enable_fb = False
    if enable_tg and not (tg_bot_token and tg_chat_id):
        log("Telegram enabled but TG_BOT_TOKEN/TG_CHAT_ID missing; disabling TG.")
        enable_tg = False

    state = load_state()
    fb_done = set(state["facebook"])
    tg_done = set(state["telegram"])

    log("Fetching recent posts from Blogger...")
    try:
        posts = fetch_recent_posts(blog_id, api_key, count=10)
    except error.HTTPError as e:
        log(f"Blogger API error {e.code}: {e.read().decode('utf-8','ignore')}")
        sys.exit(1)

    if not posts:
        log("No posts returned. Nothing to do.")
        return

    # First-ever run: seed everything except the newest `max_catchup` as done,
    # so we don't blast the whole archive to both platforms.
    if not state["seeded"]:
        log("First run -- seeding history so only newest post(s) go out.")
        for p in posts[max_catchup:]:
            fb_done.add(p["id"])
            tg_done.add(p["id"])
        state["seeded"] = True

    ordered = list(reversed(posts))  # oldest-first => natural timeline order

    fb_count = tg_count = 0
    for post in ordered:
        pid = post["id"]
        title = post.get("title", "(untitled)")
        link = post.get("url", "")
        image = extract_image(post)
        excerpt = plain_excerpt(post)

        # ---- Facebook ----
        if enable_fb and pid not in fb_done:
            log(f"FB  sharing: {title}")
            try:
                resp = post_to_facebook(fb_page_id, fb_token, title, link, image,
                                        dry_run=dry_run)
                log(f"   FB -> {resp}")
                fb_done.add(pid)
                fb_count += 1
            except error.HTTPError as e:
                log(f"   !! FB error {e.code}: {e.read().decode('utf-8','ignore')}")
            except Exception as e:  # noqa: BLE001
                log(f"   !! FB unexpected: {e}")

        # ---- Telegram ----
        if enable_tg and pid not in tg_done:
            log(f"TG  sharing: {title}")
            try:
                resp = post_to_telegram(tg_bot_token, tg_chat_id, title, link,
                                        excerpt, image, dry_run=dry_run)
                ok = resp.get("ok", True) if isinstance(resp, dict) else True
                log(f"   TG -> {resp}")
                if ok:
                    tg_done.add(pid)
                    tg_count += 1
                else:
                    log("   !! TG returned ok=false; will retry next run.")
            except error.HTTPError as e:
                log(f"   !! TG error {e.code}: {e.read().decode('utf-8','ignore')}")
            except Exception as e:  # noqa: BLE001
                log(f"   !! TG unexpected: {e}")

    state["facebook"] = list(fb_done)
    state["telegram"] = list(tg_done)
    save_state(state)
    log(f"Done. Facebook posted {fb_count}, Telegram posted {tg_count}.")


if __name__ == "__main__":
    main()
