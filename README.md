# Hooda Nutrition Uttam Nagar – Instagram repost bot

Copies new posts, reels **and stories** from **@hoodanutrition_official** and **@fitharyanvi** to **@hoodanutrition_un**:

- Phone numbers in captions → `8527150060, 8527150040`
- Najafgarh address → `Pillar No. 719, Uttam Nagar West, New Delhi 110059`
- The 3-second store outro is added to the end of every reel
- Every item comes to your Telegram first: **✅ Approve / ✏️ Edit caption / ❌ Reject**
- Approved items are posted at least 20 minutes apart
- Collab posts that appear on both accounts are posted only once
- Posts that already exist when the bot first starts are skipped (no backlog flood)

---

## 1. Instagram / Meta setup (one time)

1. **Store account → Business.** Instagram app → Settings → Account type and tools → Switch to professional → Business.
2. **Link a Facebook Page.** Create a Page for the store, then Instagram → Edit profile → Page → connect it.
3. **Create a Meta app.** developers.facebook.com → My Apps → Create App → type **Business**.
   Add the **Instagram** product, choose **API setup with Facebook login**.
4. **Get a token.** Tools → Graph API Explorer → select your app → *Get User Access Token* with:
   `instagram_basic, instagram_content_publish, pages_show_list, pages_read_engagement, business_management`
5. **Make it long-lived (60 days).** Open in a browser (fill in your values):
   ```
   https://graph.facebook.com/v26.0/oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=SHORT_TOKEN
   ```
   The bot refreshes it automatically every week after that.
6. **Find the store's Instagram user ID.** In Graph API Explorer run
   `me/accounts?fields=instagram_business_account` → copy the `instagram_business_account.id`.

The app can stay in **Development mode** – no Meta review is needed because only your own account uses it.

## 2. Telegram bot (one time)

1. In Telegram open **@BotFather** → `/newbot` → copy the bot token.
2. Put the token in `config.json`, start the bot once (step 4), and send `/id` to your bot.
   It replies with your chat id → put it in `config.json` → restart.

Commands you can send the bot: `/status`, `/check` (check the main accounts now), `/best` (top reels now), `/id`.

## 3. Raspberry Pi 5 install

```bash
sudo apt update && sudo apt install -y ffmpeg python3-venv
cd ~ && unzip hn-repost.zip && cd hn-repost
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp config.example.json config.json && nano config.json     # fill in the PUT_... values
```

Quick tests:
```bash
venv/bin/python hn_repost.py --test-caption "Call 8510868500\nPlot No-1, metro piller 86, F-Block, Indra Park,\nNajafgarh new Delhi 110043"
venv/bin/python hn_repost.py --once -v
```

Run it 24/7:
```bash
sudo cp hn-repost.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now hn-repost
journalctl -u hn-repost -f        # live log
```
(If your Pi user isn't `pi`, change `User=` and the paths in the service file.)

**On a MacBook** the same steps work (`brew install ffmpeg python`); set `"video_encoder": "h264_videotoolbox"` in `config.json` for much faster encoding.

## 3b. Stories (spare Instagram account)

Instagram's official API can't read other accounts' stories, so the bot reads them through a **spare Instagram account** (never the store account):

1. Create a new Instagram account on your phone, follow @hoodanutrition_official and @fitharyanvi, and use it normally for a day or two.
2. On the Pi, log it in once (it asks for the password and any 2FA code, and saves a session file):
   ```bash
   venv/bin/instaloader --login YOUR_SPARE_USERNAME
   ```
3. Put the username in `config.json` → `"spare_ig_username"` and restart the bot.

Stories are checked every 45 min (`story_poll_minutes`). Photo stories are turned into 6-second video stories; video stories get the outro if they stay under 60 s. Every story comes to Telegram for approval like posts do. If Instagram logs the spare account out, the bot tells you on Telegram – just repeat step 2.

## 3c. Daily top reels

Every day after 11:00 IST the bot scans the last 30 days of reels from both accounts, ranks them by views (or likes when views aren't available) plus comments, and sends you the best 2 that you haven't posted, been offered or rejected yet. Send `/best` (or `/best 5`) to the bot any time to get picks immediately. Before suggesting anything the bot cross-checks your own account (captions and thumbnails), so reels already on your page — including ones you posted yourself — are never suggested again.

## 4. Settings you may want to change (`config.json`)

| Setting | What it does |
|---|---|
| `poll_minutes` | How often the main accounts are checked (12) |
| `min_minutes_between_posts` | Gap between your posts (20) |
| `auto_post_after_minutes` | `0` = only post what you approve. e.g. `120` = post anyway if you don't answer in 2 h |
| `their_phone_numbers`, `replace_any_indian_mobile` | Which numbers get swapped. With `true`, **any** Indian mobile number in a caption is replaced |
| `address_keywords` | Words that mark the Najafgarh address line(s) |
| `extra_replacements` | Simple word swaps, e.g. `"Najafgarh": "Uttam Nagar"` |
| `stories_enabled` | `true`/`false` – turn story copying on or off |
| `story_poll_minutes` | How often stories are checked (45). Don't go below 30 |
| `best_per_day`, `best_lookback_days`, `best_hour_ist` | Daily top-reel picks: how many, how far back, what time (IST) |
| `skip_existing_on_first_run` | `true` = ignore posts that already exist when the bot first starts |

## 5. Things to know

- **Reels with licensed music / downloads turned off:** Instagram hides the video link from the API. The bot then downloads the reel with `yt-dlp` from the Pi's home internet. If that also fails you get a Telegram message with the reel link to handle manually.
- **Contact details shown inside the video** are not changed – only the caption. Check the preview before approving.
- **@fitharyanvi** also posts personal content (challenges, daily videos). Use ❌ Reject for anything that doesn't fit the store page.
- Instagram allows about 100 API posts per 24 h – far above what this needs.
- Keep `config.json` private: it contains your Instagram token and Telegram bot token.
