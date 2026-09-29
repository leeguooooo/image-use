# How image-use works (technical)

[← back to README](../README.md)

## Background: two ways OpenAI bills image generation

OpenAI offers image generation in two completely separate ways:

| Path | What you pay | How |
| --- | --- | --- |
| **Direct API** (`/v1/images/generations`) | per-image, on top of an `OPENAI_API_KEY` | curl / OpenAI SDK / etc. |
| **ChatGPT subscription** (Plus / Pro / Team) | flat monthly fee | ChatGPT web/desktop app, or the Codex CLI's built-in `image_gen` |

The subscription path is invisible to people who don't use the Codex CLI. It runs on ChatGPT's internal `backend-api/codex/responses` endpoint as a Responses-API tool, authenticated by the OAuth token written into `~/.codex/auth.json` when you run `codex login`. `image-use` exposes that capability on the command line and to any AI agent — with two backends that hit different parts of your subscription.

<img src="./two-backends.svg" width="760" alt="image-use — web vs codex backend flow">

## `web` backend (default)

Drives your logged-in browser via [`chrome-use`](https://github.com/leeguooooo/chrome-use) so generation runs on the consumer ChatGPT surface — which a headless client can't reach. The gate has three layers: Cloudflare's edge check and a sentinel proof-of-work (`backend-api/sentinel/chat-requirements` + an in-page `sentinel/sdk.js`) are both passable by a bare client, but a **Cloudflare Turnstile** token isn't — that interactive token can only come from a real browser, and it's single-use, so there's no "grab the token then go headless" shortcut. The flow:

```
image-use --backend web
   │
   ├── chrome-use open https://chatgpt.com/      (a *regular* chat — Temporary Chat disables the image tool)
   ├── resolve the ChatGPT Project (--project)    (in-page fetch: list via gizmos/snorlax/sidebar,
   │   and open chatgpt.com/g/<g-p-id>/project     create via POST /backend-api/projects if absent)
   ├── paste the prompt via stdin                 (newlines never act as Send)
   ├── verify exact editor text + all uploads     (stop if incomplete or changed)
   ├── click Send once and confirm a new message  (never replay an uncertain send)
   ├── poll the page: wait until streaming stops AND a new <img> asset is stable
   └── fetch the asset bytes in-page (credentials:'include') → base64 → save
       (the signed estuary/content URL is authorized by the browser's own cookies;
        the 2026-09 redesign serves a blob: URL instead, which only the page can read)
```

No tokens leave the browser. Each run's chat lands inside the `imagegen` Project (auto-created) instead of the top-level history; pass `--project ""` to opt out, and the conversation is deleted afterwards by default (`--keep-conversation` to keep it).

## `codex` backend

The Codex CLI's built-in `image_gen` skill is implemented as a native Responses-API tool:

```jsonc
// Codex CLI's request to chatgpt.com/backend-api/codex/responses:
{
  "model": "gpt-5.5",
  // the tool optionally takes GPT Image 2.5 knobs — model (gpt-image-2.5-sunburst/flare),
  // quality (low…xhigh/max), background, output_compression, action, partial_images
  "tools": [{"type": "image_generation"}],
  "input": [{"role": "user", "content": [{"type":"input_text","text":"draw a cat"}]}],
  // ...
}
```

The server replies with an SSE stream whose `response.output_item.done` events carry an `item.type === "image_generation_call"` payload, where `item.result` is base64 PNG. `image-use` does exactly that:

```
image-use
   │
   ├── reads ~/.codex/auth.json     (OAuth access_token, account_id, refresh_token)
   ├── reads ~/.codex/version.json  (codex CLI version → matches server expectations)
   │
   └── POST https://chatgpt.com/backend-api/codex/responses
       headers: Authorization, version, originator, session_id, …
       body:    tools: [image_generation]
       │
       └── SSE stream
           ├── response.image_generation_call.in_progress    → "queued"
           ├── response.image_generation_call.generating      → "generating"
           ├── response.image_generation_call.partial_image   → "receiving image (partial N)"
           ├── response.output_item.done  ← item.result = base64 PNG
           └── response.completed
```

If the OAuth token has expired the script auto-refreshes via `https://auth.openai.com/oauth/token` (using the refresh_token already stored by `codex login`) and persists the new token back to `~/.codex/auth.json`.

## Deep dives (blog)

- [Let Claude Code draw the image itself — the design and principles behind chatgpt-imagegen](https://blog.leeguoo.com/en/posts/chatgpt-imagegen/)
- [Why this exists + OAuth/SSE walkthrough](https://blog.misonote.com/zh/posts/chatgpt-subscription-image-api/) · [visual guide](https://blog.misonote.com/zh/posts/chatgpt-imagegen-visual-guide/) (zh; EN/JA under `/en/` and `/ja/`)
