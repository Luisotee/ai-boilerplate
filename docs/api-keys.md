# API Keys Setup Guide

This guide covers every external API key the stack can use. Only **Google Gemini** is required; every other feature switches itself off when its key is missing. `./setup.sh` asks for the common ones and writes them to the root `.env`. Keys belong in the root `.env` only, never in a package-level `.env.local` (see `.env.example`).

| Key | Required? | Used for | Cost to start |
|-----|-----------|----------|---------------|
| `GEMINI_API_KEY` | **Yes** | Chat model (or the fallback when DeepSeek is set), embeddings, text-to-speech | Enable billing |
| `DEEPSEEK_API_KEY` | No | Primary chat model, with Gemini as the fallback | Prepaid credit |
| `GROQ_API_KEY` | No | Speech-to-text (cloud Whisper) | Free tier |
| `WHISPER_BASE_URL` | No | Speech-to-text (self-hosted Whisper; no key) | Free, runs on your server |
| `LLAMA_CLOUD_API_KEY` | No | PDF parsing for the knowledge base | Free tier (1000 pages/day) |
| `JINA_API_KEY` | No | Higher rate limit for the `fetch_website` tool | Free |
| `META_*` | No | WhatsApp Cloud API client (`--profile cloud`) | Free to start |
| `TELEGRAM_*` | No | Telegram client (`--profile telegram`) | Free |

The inter-service secrets (`AI_API_KEY`, `WHATSAPP_API_KEY`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD`) are not third-party keys. `setup.sh` generates random values for them.

---

## 1. Google Gemini (required)

Gemini runs the chat agent (or backs it up when DeepSeek is configured), generates the embeddings used for conversation search and the knowledge base (`gemini-embedding-001`), and synthesizes voice replies. **Enable billing** on the Google Cloud project behind the key. The free AI Studio tier's rate limits are too low for real traffic, and embedding calls start failing under load.

### Steps

1. Go to <https://aistudio.google.com/apikey>.
2. Sign in with a Google account and accept the terms if prompted.
3. Click **Create API key** and pick (or create) the Google Cloud project it belongs to.
4. Copy the key (it starts with `AIza…`).

### Enable billing

1. Open <https://console.cloud.google.com/billing>.
2. Select the key's project and click **Link a billing account**, then add a payment method.
3. Make sure the **Generative Language API** is enabled for the project: <https://console.cloud.google.com/apis/library/generativelanguage.googleapis.com>.

### Add to `.env`

```env
GEMINI_API_KEY=AIza...
GEMINI_MODEL=gemini-3.1-flash-lite   # optional, this is the default
```

`GEMINI_MODEL` can also be changed live with `PATCH /admin/settings` (`gemini_model`), with no restart. The text-to-speech model is set separately with `TTS_MODEL`.

---

## 2. DeepSeek (optional: primary chat model)

With `DEEPSEEK_API_KEY` set, DeepSeek (`deepseek-flash`, thinking disabled) answers chats and Gemini takes over automatically on errors or timeouts. Without the key, the agent runs on Gemini alone. Embeddings and text-to-speech always stay on Gemini. **When the key is set, chat content is sent to DeepSeek's servers in China.**

### Steps

1. Create an account at <https://platform.deepseek.com> and top up some credit (DeepSeek is prepaid).
2. Create a key at <https://platform.deepseek.com/api_keys>.

### Add to `.env`

```env
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_MODEL=deepseek-flash      # optional; deepseek-v4-pro takes no image input
DEEPSEEK_TIMEOUT_SECONDS=30        # optional; seconds to start answering before Gemini takes over
```

Each fallback logs a `DeepSeek (...) failed, falling back to Gemini` warning in the worker. After an outage-type failure, DeepSeek is skipped for 60 seconds. `deepseek_model` can be changed live with `PATCH /admin/settings`.

---

## 3. Speech-to-text: Groq and/or self-hosted Whisper (optional)

Voice messages are transcribed before they reach the agent. `STT_PROVIDER` chooses the backend:

- `auto` (the default): Groq when `GROQ_API_KEY` is set, falling back to self-hosted Whisper on recoverable errors if `WHISPER_BASE_URL` is also set. When only `WHISPER_BASE_URL` is set, self-hosted Whisper runs alone
- `groq`: Groq only (needs `GROQ_API_KEY`)
- `whisper`: self-hosted only (needs `WHISPER_BASE_URL`)

With neither backend configured, `/transcribe` returns 503 and voice messages get an error reply.

### Groq (cloud)

The free tier is enough for typical voice-message volume.

1. Go to <https://console.groq.com/keys> and sign up.
2. Click **Create API Key**, name it, and copy the value (`gsk_…`).

```env
GROQ_API_KEY=gsk_...
```

### Self-hosted Whisper (no key)

The `whisper` Compose profile runs an OpenAI-compatible Whisper server (speaches) on your own hardware. A one-shot sidecar downloads `WHISPER_MODEL` on first start.

```bash
docker compose --profile whisper up -d
```

```env
WHISPER_BASE_URL=http://whisper:8000     # from inside Docker; http://127.0.0.1:8771 from the host
WHISPER_MODEL=Systran/faster-distil-whisper-large-v3
```

---

## 4. LlamaParse (optional: knowledge-base PDFs)

LlamaParse is the primary PDF parser for the knowledge base and for PDFs sent in chat. The free tier covers **1000 pages/day**.

### Steps

1. Go to <https://cloud.llamaindex.ai> and sign up.
2. Open **API Keys** in the sidebar, click **Generate New Key**, and copy it (`llx-…`).

### Add to `.env`

```env
LLAMA_CLOUD_API_KEY=llx-...
LLAMAPARSE_TIER=cost_effective     # optional: fast | cost_effective | agentic | agentic_plus
```

**Without a key**, PDFs can still be parsed locally by Docling if the image was built with it (`INSTALL_DOCLING=true`, which `setup.sh` sets when you choose Docling). `PDF_PARSER` (`auto` | `llamaparse` | `docling`) picks the parser. With no key and no Docling, PDF processing fails and chat keeps working. PDFs are parsed by the background worker. With Docling, keep `KB_MAX_CONCURRENT_PROCESSING` low because each parse can use 1–2 GB of RAM.

---

## 5. Jina Reader (optional: web fetching)

The `fetch_website` agent tool reads web pages through Jina Reader. It works without a key at **20 requests/minute**. A free key raises that to **500 RPM**.

1. Go to <https://jina.ai/reader>.
2. In the API section, click **Get Free API Key** and sign up. The key appears immediately.

```env
JINA_API_KEY=jina_...
```

---

## 6. Meta WhatsApp Cloud API (optional)

You only need this for the `whatsapp-cloud` client (`docker compose --profile cloud up -d`), which uses Meta's official API instead of, or alongside, Baileys. Follow the [Cloud API get-started guide](https://developers.facebook.com/docs/whatsapp/cloud-api/get-started). You need:

- a Meta Developer account and a **Business** app with the WhatsApp product added
- a WhatsApp Business phone number, whose **Phone number ID** is shown under WhatsApp → API Setup
- a permanent access token (a System User token from Business Settings; the temporary token on the API Setup page expires after 24 hours)
- your app secret (App Settings → Basic), which is used to verify webhook signatures
- a public HTTPS URL that points at the client's `/webhook`

```env
META_PHONE_NUMBER_ID=...
META_ACCESS_TOKEN=...
META_APP_SECRET=...
META_WEBHOOK_VERIFY_TOKEN=<any random string; enter the same value in the Meta dashboard>
META_GRAPH_API_VERSION=v23.0       # optional
```

The client checks the phone number ID and token at startup. With bad credentials it still starts, but `/health` returns 503. Free-form replies are only allowed within 24 hours of the user's last message.

---

## 7. Telegram (optional)

You only need this for the `telegram-client` (`docker compose --profile telegram up -d`).

1. In Telegram, open **@BotFather** and send `/newbot`. Pick a name and a username ending in `bot`.
2. Copy the token BotFather returns (`123456789:ABC…`).
3. To use the bot in groups, send BotFather `/setprivacy`, choose the bot, and select **Disable**. Then remove the bot from any existing groups and add it back, because Telegram only applies the setting on join.

```env
TELEGRAM_BOT_TOKEN=123456789:ABC...
TELEGRAM_WEBHOOK_SECRET=<random string of A-Z a-z 0-9 _ -, 1-256 chars; setup.sh generates one>
TELEGRAM_PUBLIC_WEBHOOK_URL=https://your-domain.example/webhook   # public HTTPS URL of the client's /webhook
```

When `TELEGRAM_PUBLIC_WEBHOOK_URL` is set, the client registers the webhook on startup. Telegram then sends the secret in `X-Telegram-Bot-Api-Secret-Token` with every update. Whitelist Telegram chats as `tg:<chat_id>` in `WHITELIST_PHONES`.

---

## Security reminders

- **Never commit `.env`.** It is already in `.gitignore`.
- If a key leaks, rotate it immediately in the provider's console.
- Set `WHITELIST_PHONES` so strangers can't spend your model quota.
- Check your billing dashboards (Google Cloud, DeepSeek, LlamaCloud) regularly during the first weeks of real traffic.
