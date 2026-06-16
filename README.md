# Blogger → Facebook + Telegram auto-share agent

Automatically shares each new Blogger post to your **Facebook Page** and your
**Telegram Channel**. Runs free on GitHub Actions — no server needed.

- **Facebook:** photo post with title + featured image + link.
- **Telegram:** photo message with bold title + excerpt + a clickable
  "पूरा पढ़ें »" link.

State is tracked per platform, so if one fails, only that one retries — the
other never double-posts. On the **first run** it posts only your newest
article and marks the rest of your history as already-shared (no spam).

---

## One-time setup

You need six secret values. Get each, then add them all as GitHub repository
secrets (last step).

### Blogger
- **`BLOG_ID`** — In Blogger, open your blog dashboard; the URL contains
  `blogID=XXXXXXXXXXXX`. That number is your blog ID.
- **`BLOGGER_API_KEY`** — At <https://console.cloud.google.com/>: create/pick a
  project → **APIs & Services → Library** → enable **Blogger API v3** →
  **Credentials → Create credentials → API key**. Copy it.

### Facebook (`FB_PAGE_ID`, `FB_PAGE_TOKEN`)
1. Create a **Business** app at <https://developers.facebook.com/apps/>.
2. In the **Graph API Explorer**, select your app, add permissions
   `pages_show_list`, `pages_manage_posts`, `pages_read_engagement`, and
   **Generate Access Token**.
3. Exchange it for a long-lived user token:
   ```
   https://graph.facebook.com/v25.0/oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=SHORT_TOKEN
   ```
4. Get your Page ID + non-expiring Page token:
   ```
   https://graph.facebook.com/v25.0/me/accounts?access_token=LONG_LIVED_USER_TOKEN
   ```
   Your page's `id` = `FB_PAGE_ID`; its `access_token` = `FB_PAGE_TOKEN`.
   Verify "Expires: Never" in the **Access Token Debugger**.

### Telegram (`TG_BOT_TOKEN`, `TG_CHAT_ID`) — the easy one
1. In Telegram, message **@BotFather** → `/newbot` → follow prompts. It gives
   you a token like `123456789:ABCdef...` → that's `TG_BOT_TOKEN`.
2. Add your bot as an **Administrator** of your channel (Channel → Manage →
   Administrators → Add Admin → your bot). It needs "Post Messages" rights.
3. `TG_CHAT_ID` is your channel's public username including the `@`, e.g.
   `@vigyankiduniya`.
   - If your channel is **private** (no username), get the numeric ID instead:
     post anything in the channel, then open
     `https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates` in a browser and
     look for `"chat":{"id":-100xxxxxxxxxx}` — use that `-100...` number.

---

## Install

1. Create a new GitHub repo (private is fine).
2. Add these files: `share_to_social.py`, `state.json`,
   `.github/workflows/share.yml`, `.gitignore`.
3. **Settings → Secrets and variables → Actions → New repository secret** and
   add all six: `BLOG_ID`, `BLOGGER_API_KEY`, `FB_PAGE_ID`, `FB_PAGE_TOKEN`,
   `TG_BOT_TOKEN`, `TG_CHAT_ID`.

## Test before going live

1. In `share.yml`, set `DRY_RUN: "true"`.
2. **Actions** tab → select the workflow → **Run workflow**.
3. Read the logs — you'll see exactly what it *would* post, nothing is sent.
4. Set `DRY_RUN: "false"` and commit. It's now live.

Local test:
```bash
export BLOG_ID=... BLOGGER_API_KEY=... FB_PAGE_ID=... FB_PAGE_TOKEN=... \
       TG_BOT_TOKEN=... TG_CHAT_ID=@yourchannel DRY_RUN=true
python share_to_social.py
```

## Tuning

- **Schedule:** edit the `cron` in `share.yml`.
- **Turn a platform off:** set `ENABLE_FB` or `ENABLE_TG` to `"false"`.
- **Catch-up limit:** `MAX_CATCHUP` (default 1) caps backlog posts per run.

## Troubleshooting

- **FB error 190** — token expired/invalid → regenerate the Page token.
- **TG 400 "chat not found"** — wrong `TG_CHAT_ID`, or bot isn't an admin of
  the channel.
- **TG 403 "not enough rights"** — give the bot "Post Messages" admin rights.
- **Telegram image fails** — the image URL must be public and < 10 MB; the
  agent falls back to a text message automatically if there's no image.
- **Pin the API version** — `GRAPH_VERSION = "v25.0"` (current Feb 2026). Bump
  it when Facebook retires the version in ~2 years. Telegram's API is
  unversioned and stable.
