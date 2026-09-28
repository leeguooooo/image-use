# image-use

[![CI](https://github.com/leeguooooo/image-use/actions/workflows/ci.yml/badge.svg)](https://github.com/leeguooooo/image-use/actions/workflows/ci.yml)

**English** | [中文](./README.zh-CN.md)

**Generate images with the subscriptions you already have — no `OPENAI_API_KEY`.**

A tiny zero-dependency Python CLI (and AI-agent skill): one file, stdlib only. It uses your ChatGPT subscription by default, falls back to Codex, and can use a Gemini subscription instead. Works on a **free ChatGPT account** too — the default backend just drives the normal ChatGPT web chat, where even free-tier users get image generation.

> **Formerly `chatgpt-imagegen`.** It was renamed once it grew non-ChatGPT backends. The `chatgpt-imagegen` command still works as an alias, and every `CHATGPT_IMAGEGEN_*` environment variable is still honoured (the new `IMAGE_USE_*` name wins if both are set). Your saved styles are untouched.

```bash
image-use "a watercolor cat sitting on a windowsill" -o cat.png
# -> saved: cat.png  (812,344 bytes)  size=1024x1024  quality=medium
```

<img width="1494" height="870" alt="image" src="https://github.com/user-attachments/assets/b48b0563-58a3-41ff-a207-f01eafbf2ccb" />

---

## Install

Needs Python 3.10+ and a ChatGPT subscription (free tier works).

**For AI agents (recommended)** — drops the skill into Claude Code, Codex, Cursor, etc.:

```bash
npx skills add leeguooooo/image-use -g
```

Then just ask: *"画一张 …"* / *"generate a hero banner for the README"*.

**Standalone CLI** — no `pip`, no virtualenv:

```bash
git clone https://github.com/leeguooooo/image-use
sudo install image-use/image-use /usr/local/bin/image-use
sudo ln -sf image-use /usr/local/bin/chatgpt-imagegen   # optional: keep the old command name
```

You also need **one backend** — `web` (default, drives your logged-in Chrome, spends no Codex-usage) or `codex` (headless fallback). `image-use doctor` shows what's ready. → **[Backends & troubleshooting](https://drawstyle.leeguoo.com/en/docs/backends)**

Got a **Gemini** subscription too? Two more backends use it instead of OpenAI: `--backend gemini` (drives a logged-in `gemini.google.com` Chrome) and `--backend agy` (the Antigravity CLI, headless). They bill **separate quotas** from each other, so either can cover for the other. Neither is ever chosen automatically — ask by name. Pin the subscribed Chrome profile with `--gemini-profile`, since most profiles are signed in to *some* Google account. Note that Gemini **text-to-image** output carries a visible watermark in the bottom-right corner (image-to-image does not), and `--size` steers the aspect ratio there rather than the exact pixel count.

## Upgrade

```bash
image-use upgrade            # install the latest release and refresh the skill
image-use upgrade --check    # only report: image-use 0.29.2 -> 0.30.0
image-use upgrade --json     # the same as JSON
```

`upgrade` (alias `update`) installs the newest GitHub release the same way this copy was installed — `skills update` for an `npx skills add` install, `git pull --ff-only` for a clone, a fresh copy of the script for a standalone file — then refreshes every other copy of the skill it finds (Claude Code plugin, clones and copies under `~/.agents/skills`, `~/.claude/skills`, `~/.codex/skills`). Any other command checks for a newer release at most once a day and prints one line to stderr when there is one. `IMAGE_USE_NO_UPDATE_CHECK=1` or the family-wide `USE_NO_UPDATE_CHECK=1` turns the check off; it is also skipped when `CI` is set. Nothing is installed until you run `upgrade`.

**On 0.23.1 or earlier?** That self-update only looked for a global `skills` and gave up when it was missing, so it cannot deliver its own fix. Bootstrap once with (installs from before the rename are registered as `chatgpt-imagegen`):

```bash
npx -y skills update chatgpt-imagegen
```

After that `image-use upgrade` works on its own.

## Usage

```bash
image-use "moody mountain sunset" -o web/hero.png --size 1536x1024
image-use "make it a warm golden-hour photo, cinematic 35mm" -i photo.jpg   # reference the subject
image-use "a different fictional person" --composition-ref photo.jpg        # borrow the framing, not the face
image-use "a robot mascot" --style doodle                                    # apply a gallery style (auto-pulled + saved)
image-use animate "a dog happily wagging its tail" --style-online snoopy --also-gif
OUT=$(image-use "icon" --quiet)                                              # capture the path
image-use "product hero" --backend codex --image-model gpt-image-2.5-sunburst --quality xhigh
```

The last line opts into the GPT Image 2.5 knobs — `--image-model`
(`sunburst` for precise editing, `flare` for fast high quality), `--quality`
(`low`→`max`), `--background transparent`, `--compression`, `--action`,
`--partial-images`. They are **codex-only** (the web/gemini surfaces have no
such controls), all optional, and are *requests* — the saved line prints the
`model=`/`quality=`/`size=` the backend actually used. Note: the **codex**
backend rewrites these server-side (observed `gpt-image-2-codex` / `auto`), so
they rarely survive there; what does matter on codex is `--model`, the *driver*
model that calls the tool (default `gpt-5.6-luna`, a fast/affordable Codex model
— a frontier coding model just burns the metered Codex bucket). Every run prints
the `tokens=` it cost.

All three below came straight out of the commands above — no retouching:

<table>
<tr>
<td width="33%"><img src="./docs/gallery/watercolor-cat.png" alt="Watercolor cat on a windowsill"></td>
<td width="33%"><img src="./docs/gallery/mountain-sunset.png" alt="Moody mountain sunset"></td>
<td width="33%"><img src="./docs/gallery/coffee-logo.png" alt="Coffee shop logo"></td>
</tr>
<tr>
<td><sub><code>"a watercolor cat sitting on a windowsill"</code></sub></td>
<td><sub><code>"moody mountain sunset" --size 1536x1024</code></sub></td>
<td><sub><code>"a coffee shop logo, circular emblem"</code></sub></td>
</tr>
</table>

`animate` asks the image model for a strict 4×2 sprite sheet, crops eight equal
frames, rejects obvious subject drift, and writes a smooth ping-pong loop. It
defaults to animated WebP; pass `--animation-format gif` or `--also-gif` when
GIF compatibility matters. The source sprite PNG is always kept beside the
animation. Animation post-processing needs
[ImageMagick](https://imagemagick.org/) (`magick`); WebP output additionally
needs [libwebp](https://developers.google.com/speed/webp/download) (`img2webp`).
`image-use doctor` reports whether both are installed.

Full options: `image-use --help`. → **[Generate images](https://drawstyle.leeguoo.com/en/docs/generate)** · **[Styles](https://drawstyle.leeguoo.com/en/docs/styles)**

The ChatGPT browser backend pastes multiline prompts, checks the exact editor
text, and waits for every reference upload to finish before clicking Send once.
Incomplete uploads or altered text stop the run. If a send cannot be confirmed,
it reports the uncertainty without sending again. Start with an empty composer;
an existing draft is preserved. A run that stops before sending clears the text
it pasted; if a run still reports a non-empty composer, clear the box at
chatgpt.com — ChatGPT restores unsent drafts in new chats.

## Community styles

Browse and reuse art styles other people tuned — a public gallery at **[drawstyle.leeguoo.com](https://drawstyle.leeguoo.com)**. No script update needed:

```bash
image-use "a fox barista" --style-online doodle  # generate with a gallery style, nothing saved
image-use style search "watercolor mascot"       # search the gallery
image-use style publish mystyle --category cute --from-last   # share yours (one-time login)
image-use upload out.png --style doodle                      # share a result to the style's player gallery (no login, on request)
```

A style can pin a **character**, not just a look. Style assets carry reference images, so the same character comes back in a brand-new scene:

```bash
image-use style add pip --kind character --ref pip-ref.png
image-use "a fox barista" --style pip
```

<table>
<tr>
<td width="50%"><img src="./docs/gallery/pip-ref.png" alt="Pip the fox — character reference"></td>
<td width="50%"><img src="./docs/gallery/pip-cafe.png" alt="Pip the fox, redrawn in a cafe scene"></td>
</tr>
<tr>
<td align="center"><sub>the pinned reference</sub></td>
<td align="center"><sub>generated from <code>"a fox barista"</code></sub></td>
</tr>
</table>

Gallery packages can be characters too — `xiaohei` is one.

→ **[Using gallery styles](https://drawstyle.leeguoo.com/en/docs/community)** · **[Submitting a style](https://drawstyle.leeguoo.com/en/docs/submit)**

## Learn more

- 📖 **[Full documentation](https://drawstyle.leeguoo.com/en/docs)** — install, generating, styles, backends, the platform.
- 🎨 **[Style gallery](https://drawstyle.leeguoo.com)** — browse and contribute community art styles.
- 📝 **[Deep dive (blog)](https://blog.leeguoo.com/en/posts/chatgpt-imagegen/)** — the design and principles behind it.
- ⚙️ **[How it works](./docs/how-it-works.md)** · **[HTTP API wrapper](https://github.com/leeguooooo/agent-cli-to-api)**

## License

MIT — see [LICENSE](./LICENSE).

## Disclaimer

This tool calls ChatGPT's internal `backend-api/codex` endpoint — the same one the official Codex CLI uses. It is not a documented public API; OpenAI could change or restrict it at any time. Use at your own risk and within the [OpenAI Terms of Use](https://openai.com/policies/row-terms-of-use/) — in particular, **do not use your ChatGPT subscription to power a public-facing image generation service**.

<details>
<summary>Keywords</summary>

`ChatGPT subscription image generation`, `free ChatGPT account image generation`, `use ChatGPT Plus for image API`, `gpt-image-2.5 without OPENAI_API_KEY`, `gpt-image-2.5 ChatGPT subscription`, `gpt-image-2 ChatGPT subscription`, `image_generation tool Responses API`, `ChatGPT image CLI`, `Codex CLI image_gen as standalone tool`, `DALL-E via ChatGPT Plus`, `OAuth-backed OpenAI image generation`, `no-API-key image generation`, `AI agent image generation skill`, `Claude Code image skill`, `OpenAI image generation without billing`.

**中文：** 用 ChatGPT 订阅生成图片、免费 ChatGPT 账号生图、ChatGPT Plus 生图工具、不用 API key 生图、gpt-image-2 用订阅、ChatGPT 订阅生图 CLI、Codex CLI 生图能力独立工具、给 AI agent 用的生图 skill、本地生图脚本、零依赖 Python 生图工具。
</details>
