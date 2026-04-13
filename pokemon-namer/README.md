# Pokemon Namer Bot

A Discord bot that watches Poketwo Pokemon spawns and identifies them using AI image classification.

## How It Works

1. The bot listens for Poketwo spawn messages (embeds with "A wild Pokémon has appeared!")
2. Downloads the Pokemon image from the embed
3. Sends it to a Hugging Face image classifier
4. Posts the name and confidence back in the channel

## Setup on Render

### 1. Create a Discord Bot

1. Go to https://discord.com/developers/applications
2. Create a **New Application**
3. Go to **Bot** tab → **Add Bot**
4. Enable **Message Content Intent** under Privileged Gateway Intents
5. Copy the **Bot Token** — you'll need this as `DISCORD_TOKEN`
6. Under **OAuth2 → URL Generator**: select `bot` scope, then `Read Messages/View Channels` + `Send Messages` permissions
7. Use the generated URL to invite the bot to your server

### 2. (Optional) Get a Hugging Face Token

Without a token the API still works but has lower rate limits.

1. Sign up at https://huggingface.co
2. Go to **Settings → Access Tokens** → **New token** (Read permission)
3. Copy the token — this is your `HF_TOKEN`

### 3. Deploy to Render

1. Push this folder to a GitHub repo (or fork this one)
2. Go to https://render.com → **New → Web Service**
3. Connect your GitHub repo
4. Set the following environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | YES | Your Discord bot token |
| `HF_TOKEN` | No | Hugging Face API token (increases rate limits) |
| `WATCH_CHANNEL_IDS` | No | Comma-separated channel IDs to watch. Leave empty to watch all channels |
| `MIN_CONFIDENCE` | No | Minimum AI confidence % to post (default: 50) |
| `DELAY_MIN` | No | Minimum random delay before responding in seconds (default: 2.0) |
| `DELAY_MAX` | No | Maximum random delay before responding in seconds (default: 4.5) |
| `COOLDOWN` | No | Cooldown between responses per channel in seconds (default: 3.0) |

5. Render will auto-detect `render.yaml` and configure everything

### 4. Get Channel IDs

To restrict the bot to specific channels (recommended):

1. In Discord: **Settings → Advanced → Developer Mode** (enable it)
2. Right-click a channel → **Copy Channel ID**
3. Add multiple IDs separated by commas: `123456789,987654321`

## Why Not Getting Flagged

- Random delay before each response (2-4.5 seconds by default)
- Per-channel cooldown prevents rapid-fire responses
- Concurrency semaphore limits simultaneous requests
- Responds as a reply to the spawn message (natural behavior)
- Clean startup with proper HTTP health endpoint for Render

## Commands

| Command | Description |
|---------|-------------|
| `!ping` | Check if the bot is alive |

## Troubleshooting

**Bot isn't responding:**
- Make sure Message Content Intent is enabled on the Discord Developer Portal
- Check that the bot has permission to read and send messages in the channel
- If using `WATCH_CHANNEL_IDS`, make sure the channel ID is correct

**"Not sure!" all the time:**
- The Hugging Face model may be loading (cold start) — wait a minute and try again
- Lower `MIN_CONFIDENCE` to 30 if you want it to post lower-confidence guesses
- Add a `HF_TOKEN` for better reliability

**Getting flagged:**
- Increase `DELAY_MIN` and `DELAY_MAX`
- Increase `COOLDOWN`
- Make sure you're using a proper Bot account (not a user/selfbot)
