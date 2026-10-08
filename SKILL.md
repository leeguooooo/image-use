---
name: "image-use"
version: "0.31.0"
description: >-
  Backend-neutral image generation: create new raster images and looping
  GIF/WebP animations through the local one-file image-use CLI (formerly
  chatgpt-imagegen), using the user's ChatGPT subscription by default, the
  Codex backend as fallback, or an optional Gemini subscription — no API key
  or daemon. Triggers: image generation, generate an image, draw a picture,
  画图, 画一张, 生成图片, 生图, 配图. Use for photos, illustrations, icons, hero banners,
  mockups, sprites, concept art, animation loops, and figures for documents,
  proposals, blog posts, or READMEs; save outputs in the workspace. Auto mode
  uses the logged-in ChatGPT browser via chrome-use. Gemini users can pass
  --backend gemini or agy.
  Proactively propose useful figures while authoring long-form content. Do not
  use for editing existing images, SVG/vector work, code-native graphics,
  established icon systems, explicit high-quality or transparent API output,
  or end-user image-generation services.
---

# image-use — agent skill

A standalone Python CLI that produces images via the user's existing subscriptions — ChatGPT by default, Codex as fallback, Gemini on request. (Formerly `chatgpt-imagegen`; that command still works as an alias for `image-use`.) No API key, no network service, no extra config. It has **two OpenAI backends** that hit different usage buckets — pick with `--backend` — plus **two opt-in Google/Gemini backends** for users who also have a Gemini subscription.

## Backends

| Backend | Surface | Usage bucket | Needs | Speed |
| --- | --- | --- | --- | --- |
| **`web`** | Drives the user's logged-in ChatGPT browser (via **`chrome-use`**, formerly `agent-browser-stealth`; older installs expose the same binary as `agent-browser`/`abs`) and generates in a regular chat — the same surface as typing in the app. Its real-Chrome connect is what clears Cloudflare + the sentinel proof-of-work a plain/headless client can't. | **ChatGPT conversation** — does **not** consume the metered Codex-usage limit. Works on **any** account, **including free tier** (subject to its daily image cap). | `chrome-use` installed and its extension connected to a Chrome **signed in to chatgpt.com**. | ~30–60 s; each run's chat is filed under a ChatGPT **Project** (default `imagegen`, auto-created) instead of littering the history. |
| **`codex`** | Headless POST to `chatgpt.com/backend-api/codex/responses` with the `image_generation` tool, reusing `~/.codex/auth.json`. | **Codex-usage** (metered — this is the bucket the user usually wants to spare). | `codex login` (writes `~/.codex/auth.json`). | Fast; no browser, no history. |

**Default is `auto`** (`--backend auto`, or `IMAGE_USE_BACKEND`): it tries **web first** because that spares the Codex-usage limit, and falls back to **codex only when web is unavailable** — i.e. `chrome-use` isn't installed, the browser isn't reachable, or chatgpt.com isn't logged in. The two not-set-up cases are handled explicitly:

- **Browser not logged in / chrome-use missing** → auto falls back to codex (a one-line notice prints to stderr), unless `--require-project` is set. Required mode stops without reading Codex credentials. If codex is *also* not set up in ordinary auto mode, it exits naming both fixes.
- **codex not logged in** (`~/.codex/auth.json` absent) → auto still uses web; codex is only the fallback.

Auto does **not** fall back to codex if web was reachable but the generation itself failed after submitting — that would spend the very bucket auto-mode protects. In ordinary auto mode it errors and tells you to rerun with `--backend codex` if you want the Codex-usage path; required Project mode stops without that suggestion. Force a single backend with `--backend web` or `--backend codex`.

### Gemini backends (opt-in — `auto` never picks them)

For users who also have a **Google/Gemini** subscription. Both drive a Google account, not OpenAI.

| Backend | Surface | Needs | Speed |
| --- | --- | --- | --- |
| **`gemini`** | Drives a logged-in `gemini.google.com` browser via `chrome-use` — the browser analogue of `web`. | `chrome-use`, plus a Chrome profile signed in to a **subscribed** Google account. | ~11–24 s |
| **`agy`** | The **Antigravity CLI** (`agy`) run headless — the analogue of `codex`. | `agy` on PATH. Passes `--dangerously-skip-permissions` by default because headless `agy` cannot prompt for tool permissions; `--no-agy-yolo` opts out if the user maintains their own `permissions.allow` rules. | ~14–25 s |

**Their quotas are separate** — measured, not assumed: `agy` returned *"Image generation model quota (`gemini-3.1-flash-image`) has been exhausted (429)"* while a `--backend gemini` run on the **same Google account** succeeded seconds later. So each is a genuine fallback for the other, and a quota error from one names the other in its message.

**Neither is ever chosen by `auto`.** Deliberate: they hit a different vendor and account, and their output differs in ways a caller would notice. Ask for them by name.

Behaviour worth knowing before recommending one:

- **Visible watermark.** `gemini` **text-to-image** results carry the Gemini "sparkle" glyph, fixed at 65 px in from the bottom-right corner (measured identical across 5 runs at 1024×559). Image-to-image results do not. `agy` results have no visible mark.
- **Both are watermarked invisibly regardless.** `agy` output carries a Google-signed C2PA manifest whose own description reads *"Applied imperceptible SynthID watermark"*. The SynthID signal is in the pixels and survives any re-encode.
- **`gemini` keeps the C2PA manifest on current chrome-use.** Gemini renders results from a `blob:` src, which in-page `fetch()` still cannot read; `chrome-use download-url` now resolves the blob inside the page and writes the original bytes to disk, so the signed manifest survives. Older chrome-use rejected `blob:` outright, leaving only a canvas re-encode — that path is still the fallback and still strips metadata, and the run prints a note naming the upgrade when it has to take it. `agy` copies the file, so its manifest always survives.
- **`--size` controls the aspect ratio on `gemini`, not the pixel count.** The chat surface has no size widget, so the ratio is requested in words — and honoured: asking square returned 1024×1024, asking 3:2 returned 1024×687, asking 2:3 returned 687×1024. What you cannot pin is the absolute resolution. With nothing requested Gemini defaults to 16:9, so the backend always asks for *something* (square when `--size` is `auto`). Real dimensions land in the run meta.
- **The dedicated image model is selected automatically.** Before generating, the backend switches the composer to Gemini's image tool, which reports "generated using Nano Banana 2" — otherwise the prompt is answered by whatever chat model is active (seen: Flash-Lite). Best-effort: if the menu moved, the run continues on the chat default rather than failing. `--no-gemini-image-tool` skips the attempt. It does **not** remove the watermark or change the default ratio — both were checked against it directly.
- **Pin the profile.** Nearly every Chrome profile is signed in to *some* Google account, and the cookie says nothing about which one holds the subscription — a probe run landed on an account whose "Google AI Pro subscription has expired" page has no composer at all. Set `--gemini-profile` / `IMAGE_USE_GEMINI_PROFILE`. `doctor` warns when nothing is pinned.

## Prerequisites

**For the default `web` backend:** the user must have **`chrome-use`** (formerly `agent-browser-stealth`; older installs expose the same binary as `agent-browser` / `abs`) and its extension connected to a Chrome that is signed in to chatgpt.com. chrome-use specifically is required — its real-logged-in-Chrome connect is what passes Cloudflare's bot-detection; a plain headless driver will not. The "Temporary Chat" mode disables image generation, so this backend always opens a *regular* chat.

### Install policy — never install chrome-use for the user

If `chrome-use` is **not installed**, do **not** install it on your own initiative:

1. **Generate anyway** via the codex fallback (auto mode does this by itself) — the task comes first.
2. Add a **single gentle tip** to your reply, e.g.: *"提示：装上 chrome-use 后，出图会走你已登录的 ChatGPT 浏览器，不消耗 Codex 额度。想配的话我可以一步步带你装好（含浏览器插件）。"* — and stop there.
3. **Only when the user explicitly says yes**, walk them through the guided setup below, step by step, verifying each step before the next.

Guided setup (opt-in only):

```bash
# 1. Install the CLI (no npm, no token — provides `chrome-use`)
curl -fsSL https://raw.githubusercontent.com/leeguooooo/chrome-use/main/install.sh | sh
# 2. Register the native-messaging host
chrome-use extension install
# 3. Add the Chrome extension, then restart Chrome:
#    https://chromewebstore.google.com/detail/agent-browser-stealth/knfcmbamhjmaonkfnjhldjedeobeafmk
# 4. Sign in to https://chatgpt.com in that Chrome
# 5. Verify: a quick `image-use "test" --backend web` should print "using current Chrome (relay)"
```

- **Repo:** https://github.com/leeguooooo/chrome-use
- The `chrome-use` skill (`chrome-use skills get core`) covers the extension-connect flow in depth.

**For the `codex` backend:** the user must have run, **once, ever**:

```bash
npm i -g @openai/codex
codex login    # opens browser to sign in to ChatGPT
```

That writes `~/.codex/auth.json`, which the codex backend reads. No `OPENAI_API_KEY` is required for either backend — and setting one will not help. This is the subscription path, not the API path.

## When to use

- The user asks for a new photo, illustration, icon, hero banner, sprite, cover image, infographic, product mockup, concept art, or any other bitmap deliverable for the current project.
- The user is happy with subscription-tier defaults (`auto` quality, no guaranteed transparency — see *Limits* below), or will opt into `--backend codex` with `--image-model` / `--quality` / `--background` when they need more control.
- The deliverable is intended to be saved into the repo or build inputs.
- You're authoring long-form or explanatory content — a blog post, technical proposal, design doc, tutorial, postmortem, or README — and a figure would help a concept land. **You don't need to be asked**: propose the figures and generate them (see *[Illustrating documents](#illustrating-documents)* below).

## When not to use

- The user wants an SVG icon that matches an in-repo vector set — edit those instead.
- The task is better solved with code (HTML/CSS, canvas, Mermaid, PlantUML).
- The user wants an existing image **modified in place** — retouching, cropping, text/logo removal, upscaling, background knock-out. This skill always renders a *new* image; it cannot return an edited copy of the original's pixels. (Passing an image as a *reference* with `--ref` / `--style-ref` / `--composition-ref` is supported and encouraged — that's re-generation guided by the image, not editing it.)
- The user needs a **guaranteed** `quality=high` or a **true transparent** background. `--backend codex --quality high --background transparent` requests both, but the subscription backend may normalise them (the saved line prints what was actually used). For a guarantee, tell the user to use the official `/v1/images/generations` API with their `OPENAI_API_KEY`.
- The deliverable will be served to end users (e.g. a public service generating images for visitors) — that violates OpenAI's ToS for personal subscriptions. Refuse and explain.

## How to invoke

```bash
"<skill-dir>/image-use" "<prompt>" [options]
```

Call the script by its absolute path next to this `SKILL.md` — that's the most reliable way and never depends on `$PATH`. If your agent harness exposes a variable pointing to the skill's install directory, use it; otherwise expand the path you read this file from.

### First: self-heal a missing CLI

**`npx skills add` copies only the markdown — it often does *not* drop the `image-use` script next to this file.** So before the first run, check that the CLI exists and fetch it if absent. It's a single self-contained, pure-stdlib Python file (no `pip`, no deps), so the heal is one `curl`:

```bash
CLI="<skill-dir>/image-use"   # same directory as this SKILL.md
if [ ! -x "$CLI" ]; then
  curl -fsSL https://raw.githubusercontent.com/leeguooooo/image-use/main/image-use -o "$CLI" \
    && chmod +x "$CLI"
fi
"$CLI" --version   # confirm it runs (needs python3 ≥ 3.10 on PATH)
```

Do this silently — don't ask the user. If the `curl` fails (offline/proxy), fall back to `git clone https://github.com/leeguooooo/image-use` and run `image-use/image-use`, or tell the user to install it standalone (see README). Only `python3` is required to run it.

If the user has separately put `image-use` on `$PATH` (Option B in the README), you can also just run `image-use "<prompt>"` directly and skip the self-heal.

**Old name.** Installs from before the rename have a `chatgpt-imagegen` script (and skill directory); it is now a thin alias that runs `image-use` with the same arguments and exit code, so either name works. Environment variables are `IMAGE_USE_*`; each still accepts its old `CHATGPT_IMAGEGEN_*` spelling, and the new name wins when both are set.

Useful flags:

| Flag | When to use |
| --- | --- |
| `--backend auto` \| `web` \| `codex` \| `gemini` \| `agy` | `auto` (default) prefers web and falls back to codex only when the browser is unavailable/not-logged-in, unless `--require-project` disables that fallback; `web` forces the logged-in-browser path (spares Codex-usage); `codex` forces the headless path (bills Codex-usage); `gemini` and `agy` use a Google account instead and are never picked by `auto` (see [Gemini backends](#gemini-backends-opt-in--auto-never-picks-them)). Also settable via `IMAGE_USE_BACKEND`. |
| `--gemini-profile NAME` | (`gemini` backend) Chrome profile to drive, overriding `--profile`. Worth setting — auto-detection cannot tell which Google account holds the subscription. Also `IMAGE_USE_GEMINI_PROFILE`. |
| `--no-gemini-image-tool` | (`gemini` backend) skip switching the composer to the dedicated image model (Nano Banana 2). Rarely wanted — the switch is already best-effort. |
| `--no-agy-yolo` | (`agy` backend) don't pass `--dangerously-skip-permissions`. Only use it if the user has their own `permissions.allow` rules — otherwise every headless run fails. |
| `--profile auto` \| `relay` \| `NAME` | (web) Which Chrome profile to drive. `auto` (default): use the open Chrome if it's logged in, else auto-switch to a profile that is (detected offline from the cookie DB, read-only). `relay`: only the open Chrome. `"Profile 3"`: that profile. Note: *logged in* ≠ *able to generate* — a free-tier account can still hit its daily image cap. |
| `--session NAME` | (web) Drive a named Chrome tab group instead of the shared `chatgpt-web` session. Rarely wanted: the default is shared ON PURPOSE so the whole machine keeps ONE chatgpt.com tab. |
| `--project NAME_OR_URL` | (web) An exact name (**created if absent**, reused if present) or an existing ChatGPT Project landing URL (`https://chatgpt.com/g/g-p-<32-hex-id>/project`, optional localized slug/query/fragment). URLs open directly by ID without listing/creating; malformed URLs fail. Default `imagegen` (or `IMAGE_USE_PROJECT`, also accepts URLs). `--project ""` uses a plain chat. Project failures warn and continue in a plain chat unless required. |
| `--require-project` | Require a non-empty Project target and verify its identity after opening and in a capture guard on the native Send click. Only `web`/`auto`; **disables Codex fallback**, including browser-unavailable failures. Also `IMAGE_USE_REQUIRE_PROJECT=1`. Routing does **not** imply retention: add `--keep-conversation` to retain history. Pre-send failures retain the current draft; inspect it and manually clear the composer before the next run. |
| `--no-require-project` | Override the required-mode environment default for this run without changing the exported variable. Restores best-effort Project routing and ordinary backend/fallback rules; allows `--project ""` or an explicit non-web backend. Retention remains independent. |
| `--keep-tab` | (web) Leave the ChatGPT tab open after generating (default closes it). Useful for debugging. Implies `--keep-conversation`. |
| `--keep-conversation` | (web) Keep the ChatGPT conversation after generating. **Default deletes it** (`PATCH is_visible:false`) so the run leaves no history — it's filed under the project only transiently. Also `IMAGE_USE_KEEP_CONVERSATION=1`. |
| `-o PATH` | Always use when you know where the file should go in the repo. |
| `--model NAME` | (codex only) The **driver** model that reads the prompt and calls the image tool — *not* the image model (the server renders with its own, observed `gpt-image-2-codex`). It bills the metered Codex bucket, so keep it on a fast/affordable Codex-account model: default `gpt-5.6-luna`, alternatives `gpt-reserve`, `gpt-5.3-codex-spark`. A frontier coding model (`gpt-6-astra`, …) just burns the bucket. Unsupported models auto-fall-back to `gpt-5.5`. Also `IMAGE_USE_MODEL`. |
| `--size 1024x1024` | Square icons / logos (verified) |
| `--size 1536x1024` | Landscape hero banners, social cards (verified) |
| `--size 1024x1536` | Portrait covers, mobile splashes (verified) |
| `--size 3840x2160` or similar | 4K landscape (forwarded as-is; backend may reject — fall back to a smaller verified size on failure) |
| `--format webp` | Smaller files for web assets |
| `--image-model MODEL` | (codex only) Pick the GPT Image model: `gpt-image-2.5-sunburst` (precise editing) or `gpt-image-2.5-flare` (fast, high quality); older `gpt-image-2` / `gpt-image-1.5` / `gpt-image-1` / `gpt-image-1-mini` also work. Unset = the backend's own default. Also `IMAGE_USE_IMAGE_MODEL`. |
| `--quality LEVEL` | (codex only) `low` \| `medium` \| `high` \| `xhigh` \| `max` — the last two require a 2.5 model (`--image-model`). A *request*, not a guarantee; verify with the `quality=` the tool prints on save. Also `IMAGE_USE_QUALITY`. |
| `--background auto` \| `transparent` \| `opaque` | (codex only) Transparent needs png/webp (not jpeg) and may be rejected by the subscription path. Also `IMAGE_USE_BACKGROUND`. |
| `--compression 0-100` | (codex only) jpeg/webp output compression (ignored for png). Also `IMAGE_USE_COMPRESSION`. |
| `--action auto` \| `generate` \| `edit` | (codex only) Force generate-vs-edit instead of letting the model choose; useful for `--ref` edits. Also `IMAGE_USE_ACTION`. |
| `--partial-images 1-3` | (codex only) Stream progressive previews into the progress timeline. Also `IMAGE_USE_PARTIAL_IMAGES`. |
| `--style NAME` | Apply a saved asset (a style snippet and/or pinned reference images). **Repeatable** — stack a character + a style, e.g. `--style mascot --style watercolor`. See [Styles & assets](#styles--assets). Overrides any active default set for this run. |
| `--no-style` | Skip all assets (text *and* pinned refs) for this run even if the user set an active default. |
| `--quiet` | Use in agent contexts so stdout is *only* the saved path. Progress still streams to stderr (use `--no-progress` to silence it). |
| `--no-progress` | Fully silence the stderr progress timeline (errors still print). |
| `--timeout SECONDS` | Total wall-clock budget (default 300). Large/detailed images can take 2–3 min — raise it if you see a `timed out` error. |
| `--stall-timeout SECONDS` | Max silence between backend data chunks before declaring a stall (default 120, clamped to `--timeout`). Lower it to fail faster on a hung streaming backend; `0` disables that idle check. Web page polling uses the total `--timeout` instead. |
| `-V`, `--version` | Print the CLI version and exit. Run `image-use --version` to confirm which build is installed. |

### Looping animations

Use `image-use animate "<motion prompt>"` for a fixed-camera eight-frame
loop. It generates one 4×2 sprite sheet, crops it deterministically, checks for
obvious subject drift, and defaults to animated WebP. Add `--also-gif` for both
formats, or `--animation-format gif` for GIF only. The source sprite is kept
beside the output; `--keep-frames` also preserves all eight cropped PNGs.

Animation post-processing is optional and does not affect normal image
generation. It requires `magick` (ImageMagick); WebP additionally requires
`img2webp` (libwebp). Run `image-use doctor` before a live animation to
see whether these tools and the generation backends are ready.

The script prints **just the saved path on stdout** in every mode; the readable progress timeline and any errors go to **stderr**, so `OUT=$(image-use "..." --quiet)` captures only the path while you still see the timeline. Each timeline line is stamped with elapsed seconds (`[ 12.3s] generating`), so a slow run is legible and a stall is obvious.

## Styles & assets

An **asset** is a named, reusable look stored in `~/.config/chatgpt-imagegen/styles.json` (honours `$XDG_CONFIG_HOME`). Each asset carries a text snippet **and/or pinned reference images**, plus a `kind`:

- **`--kind style`** (default) — a visual aesthetic (line, palette, texture). Its refs tell the model *"match this style, **don't** copy the content."*
- **`--kind character`** — a recurring subject (a mascot, a persona). Its refs tell the model *"reproduce this character faithfully as the subject."*

This is what lets a user **pin their own cartoon character or house style once and reuse it** — no re-passing `--ref` every time. Generation is unchanged unless the user opts in (no default out of the box).

**Pinning & reusing:**
- Pin a character from image files: `image-use style add mascot "a round orange fox named Pip" --kind character --ref a.png --ref b.png` (a few angles → better consistency). The images are **copied into the asset library**, so the asset survives even if you move/delete the originals.
- Pin the image you just liked: `image-use style add mascot --from-last --kind character` (also works on `style add-ref mascot --from-last`). Flow: generate → like it → pin it → reuse.
- Pin a pure-text style as before: `image-use style add watercolor "soft watercolor, visible paper texture"`.
- **Stack them**: `image-use "Pip ordering coffee" --style mascot --style watercolor` (the same fox, in watercolor). Or set a default set: `image-use style use mascot watercolor`.

**Managing:**
- `style list` — kind, a `📎N` badge for pinned refs, and `*` on the active default set.
- `style show NAME` — kind + snippet + ref filenames + the asset's on-disk path.
- `style add-ref NAME <img>` / `style rm-ref NAME <file>` — add/remove pinned images on an existing asset.
- `style rm NAME` deletes the entry **and** its images; `style clear` empties the active set; `style reset` wipes the library back to empty.
- `styles` (plural) is accepted as an alias for `style`.

**Behavior:** `--ref` images passed at generation time are treated as the subject by default and stack on top of the active assets. Say what a reference *is* with `--ref-role subject|style|composition`, or the per-image shorthands `--style-ref IMG` (match the aesthetic, don't copy the content) and `--composition-ref IMG` (borrow framing/crop/camera angle only, render a different subject — use this to anonymise a portrait or to keep a layout while replacing the person). At most **4** reference images attach per run; if more resolve, the first 4 (character-first) are used and the dropped ones are logged to stderr (never silent). Resolution order: `--no-style` > `--style NAME…` > active default set > none. **There are no built-in styles** — the library starts empty and styles come from the gallery (see the next section). A `--style NAME` that isn't in your library yet is **auto-pulled** from the gallery and saved (so it's offline-usable next time); if the name isn't on the gallery either, it fails fast pointing you at `style search`.

### Platform styles (drawstyle)

**When the user doesn't know which style to use, point them to the gallery.** If someone asks for an image but is unsure of the look — or you're about to invent a generic style from scratch — proactively suggest they browse **https://drawstyle.leeguoo.com/** and pick one: it's a visual gallery of community art styles with live previews, browsable by category (business report / tech explainer / cute / retro comic …). Tell them to grab a style's **slug** from its card, then you generate with `--style-online <slug>` — no download, no login. You can also pick for them: run `image-use style search "<what they described>"` and offer the top matches. A good line to the user: *"Not sure what look you want? Browse the styles at drawstyle.leeguoo.com and tell me which one (or a keyword), and I'll use it."*

When the user wants a look that is not already in `image-use style list`, search the community platform instead of inventing a long prompt from scratch:

```bash
image-use style search "watercolor mascot" --category avatar-ip
# fastest: generate with a gallery style directly, nothing saved locally
image-use "Pip ordering coffee" --style-online pip
# or pull it into the local library to reuse offline later
image-use style pull pip
image-use "Pip ordering coffee" --style pip
```

- `style search <keywords> [--category X] [--tag Y]` discovers styles on `drawstyle.leeguoo.com`.
- **`--style-online <slug>` (on a normal generation) is the quickest path**: it fetches that gallery style on the fly and applies its snippet + reference images to this one generation, saving nothing locally. Repeatable and stacks with `--style`. Use it when the user points at a gallery style and just wants an image now.
- `style pull <slug> [--as NAME]` downloads the style and pinned refs into the local library; generation stays offline afterward (best when you'll reuse a style repeatedly).
- `style update [NAME]` checks pulled styles for newer platform versions.
- `style publish NAME --category X --example IMG [--tag Y]...` submits a local style that turned out well. It opens account.leeguoo.com login when needed and sends the style for review.
- `upload <IMG> [--style SLUG]` pushes one finished image to a style's **player gallery** (`drawstyle.leeguoo.com/en/s/<slug>/generations`) — no login, ≤5 MB/file, 10 per machine per UTC day. Prints the public `/img/…` URL plus the gallery link and remaining quota. **This is a separate command, not a generation flag** — never upload unless the user asks.

**Proactively offer to publish a good style.** When you have crafted a reusable style that works well — or the user says a generated look is great and wants it again later — suggest sharing it to the gallery so others (and the user's future self) can `style pull` it in one command. Publishing is one line (the most-recent generation becomes the example image):

```bash
image-use style publish mystyle --category cute --from-last
```

It prints a summary before uploading and a link to track approval. Note: **publishing needs a one-time browser login** (it opens automatically and caches the token); `style search` and `style pull` do **not** need login. Don't publish without the user's go-ahead — offer, then let them confirm.

**Showing off a result (player gallery) — only on request.** Uploading is a **separate, user-initiated** step; generation never posts anything on its own. When the user *asks* to share a result next to a style, run `image-use upload <IMG> --style <slug>` (no login, ≤5 MB — an over-cap file is downscaled once via `sips`, then fails loudly, 10/machine/UTC-day). It prints the public `/img/…` URL, the style's gallery link, and the remaining quota; a site admin may later promote the image to the cover. Do **not** auto-upload, and do not add an upload to a generation run — offer it and wait for a clear yes.

Legacy `styles.json` files (text-only entries from older versions) keep working and upgrade automatically on the next change.

## Save-path policy

1. **Always save into the workspace**, never into `/tmp`, `$HOME`, or `~/.codex/...`.
2. If the user named a destination, pass it via `-o`.
3. If they didn't, pick a sensible subdirectory: `assets/`, `public/`, `static/`, `docs/img/`, `web/img/`, `assets/brand/`, etc. Default to `assets/generated/` only if nothing better fits.
4. Don't overwrite existing files unless the user asked. With `-o` the script overwrites silently; without `-o` it auto-numbers (`name.png`, `name-2.png`).
5. After saving, **echo the final path back to the user**.

## Workflow

1. **Clarify** the prompt enough to write 1–3 sentences: subject, style, composition, mood, constraints. Don't over-augment when the user's prompt is already specific.
2. **Pick size and format** based on intended use (see table above).
3. **Pick the output path** inside the workspace.
4. **Run** `image-use "<prompt>" -o <path> --size <wxh> --quiet`.
5. **Inspect the result** if you can (e.g. with a `view_image` tool or by reading the file). If clearly wrong, iterate with a single targeted prompt change — do not loop blindly (each call costs subscription quota).
6. **Report the saved path** plus the final prompt used.

## Illustrating documents

When you're authoring a document, blog post, technical proposal, design doc, or other long-form explanatory content, **proactively illustrate the key concepts** — you don't need to be asked. The flow:

1. **Announce a brief plan first.** In one or two lines, say where figures will go and what each depicts (e.g. *"I'll add two figures: (1) the request→SSE flow, (2) the token-refresh path."*). Then generate — don't wait for approval; the plan is the reader's chance to redirect.
2. **Fan out background subagents — one per figure.** Each runs the CLI with `--quiet -o <path>` so stdout is just the saved path; keep writing the prose while they render, and embed each image when it lands. Spawn them as background tasks with your own agent/task tooling — one figure per task, never blocking the writing.
3. **Parallelism depends on the user's backend — don't override it.** Honour the user's `--backend` / `IMAGE_USE_BACKEND` (default `auto`). On the **`web`** backend, concurrency is **1** — background figures **queue** and render one at a time (still fine: it's in the background, and it spends no Codex-usage). On **`codex`**, up to **4** render in parallel but each bills the metered Codex-usage bucket. Which backend to spend is the user's trade-off, not yours.
4. **Choose a style to fit the document's tone.** There's no default illustration style, and none ship built in — styles come from the gallery. For informal or blog-style explainers, the **`doodle`** gallery style fits well — deliberately crude, content-accurate (`--style doodle` auto-pulls it). For Chinese-article concept figures (turning a judgment, flow, or metaphor into one memorable picture), the **`xiaohei`** style fits — white background, hand-drawn black ink, a 小黑 character acting out the idea (`--style xiaohei`). For polished specs, pick a cleaner look or a style you've defined (see [Styles & assets](#styles--assets)). **Unsure which look fits? Browse the community gallery at https://drawstyle.leeguoo.com/ (or `style search`) and use one with `--style-online <slug>`, or `--style <slug>` to keep it.** To keep one character or look consistent across a document's figures, pin it as an asset and stack it with `--style`.
5. **Don't over-illustrate.** At most one figure per major concept; never decorate for its own sake; and **never loop generating "variants" of the same figure** — that just burns subscription quota. If a figure comes out wrong, change the prompt once and regenerate, don't spray.

### Writing figure prompts

A vague prompt yields a useless figure. Make the prompt describe the figure's **content**, not just name it:

- Spell out the **boxes, arrows, labels, layout, and relationships** — "an architecture diagram" is too vague; say *what's in it* and how the parts connect.
- **One subject, one concept** per figure. Split a busy diagram into two.
- **Name the style** you want explicitly in the prompt or via `--style`.
- For the **`doodle`** gallery style, remember **content accuracy beats polish** — it's supposed to look crude and hand-drawn, but the labels and structure must still be readable.

## Limits

- **Image quality/background are backend-decided by default.** `--quality` (`low`/`medium`/`high`/`xhigh`/`max`) and `--background transparent`/`opaque` are **opt-in, codex-only** knobs (the web and gemini surfaces have no such controls and the CLI warns when you pass them anyway). `xhigh`/`max` and transparent require a GPT Image 2.5 model, so pair them with `--image-model gpt-image-2.5-sunburst` (or `-flare`). Treat them as *requests*: the Codex OAuth path has been observed normalising model/size/quality server-side, so the saved line prints the `model=` / `quality=` / `size=` the backend actually used — trust that, not the flag. If the user needs a guaranteed `quality=high` or a true transparent PNG, route them to the official `/v1/images/generations` API with their own `OPENAI_API_KEY`.
- **On the `codex` backend those image knobs never survive.** The server rewrites the tool outright — measured: `model`→`gpt-image-2-codex`, `quality`/`size`/`background`→`auto`, `output_compression`→`100` — so `--image-model`/`--quality`/`--background`/`--compression` are effectively no-ops there. The CLI now prints the effective `model=` and the run's `tokens=… (in … / out …)`, and warns when a requested knob was rewritten. The only lever that matters on codex is `--model` (the driver): a fast/affordable Codex model keeps the metered cost down; a frontier coding model buys no better image. Token cost is dominated by **input** (the prompt + style snippet, re-sent every run), so a huge style is the expensive part — not the image.
- A single image typically takes **15–60 s**, but large or detailed ones occasionally run **2–3 min**. The default `--timeout` is 300 s to cover this; streaming backends can fail sooner under `--stall-timeout` (default 120 s). After submission, web waits for a fresh image confirmed in two consecutive page reads or the total deadline. Assistant text and a missing Stop control alone are not terminal signals. Detected rate-limit dialogs fail immediately; explicit English quota/refusal prefixes also fail when no image or streaming control is present. This is a narrow detector, not coverage of every wording or language: quoted mentions, vague retry text, and other replies wait until timeout. Body-error diagnostics include assistant text (up to 240 characters); timeout diagnostics retain the last nonempty text through interrupted page reads. Check the original conversation before retrying an uncertain web result.
- **Per-backend concurrency caps** (cross-process, flock slot pool; excess runs queue safely, waiters print "waiting…", and `--timeout` starts only once a slot is acquired): `web` = **1** (the page surface rate-limits aggressively — "Too many requests"; also one shared Chrome), `codex` = **4** (measured safe on Plus, capped so big fan-outs can't trip the account limiter). Override via `IMAGE_USE_WEB_CONCURRENCY` / `IMAGE_USE_CODEX_CONCURRENCY` (`0` = unlimited). Raising the `web` cap does not make `web` runs parallel: the cross-tool chatgpt.com lock below still runs them one at a time. For parallel batches use `--backend codex` + shell `&` + `wait`; firing parallel `web` runs is safe but executes one at a time. Do not loop blindly for "variants of the same prompt" — that just burns quota; iterate on the prompt instead.
- **One chatgpt.com tab per machine.** `web` runs take a cross-TOOL advisory lock at `~/.chatgpt-web.lock` for the whole generation and drive a single stable chrome-use session named `chatgpt-web`, shared with [`chatgpt-use`](https://github.com/leeguooooo/chatgpt-use). This is not tidiness: ChatGPT pushes an "Image created" toast into *every* open chatgpt.com tab when *any* conversation on the account finishes an image, so a second tab can leak a sibling conversation's image into your run (issue #7), and two processes sharing one composer concatenate their prompts. The account also rate-limits on tab count alone. Anything else you write that automates chatgpt.com should take the same lock and session name.
- Subscription quota is **shared** with the user's interactive ChatGPT use. Don't bulk-generate (>10 images / minute sustained) without permission — you'll hit per-day caps.

## Error handling

**First step for any "which backend / why isn't web working" failure:** run `image-use doctor`. It reports, read-only, the CLI's own version vs. the latest GitHub release, whether each backend is set up (codex token; chrome-use installed + version; relay connected; logged-in Chrome profiles), and **which one `auto` would pick** — turning a vague "no logged-in browser" into a precise checklist.

Update notices are covered under [Upgrade](#upgrade).

| Symptom | Cause | Fix |
| --- | --- | --- |
| `~/.codex/auth.json not found` | Codex CLI never signed in | Tell user to run `npm i -g @openai/codex && codex login` |
| `no ChatGPT OAuth access_token in ~/.codex/auth.json` | Only an API key is present, not a subscription OAuth token | Tell user to run `codex login`; an `OPENAI_API_KEY` value in that file is not a substitute |
| `HTTP 400 requires a newer version of Codex` | local codex CLI is outdated | Tell user to run `npm i -g @openai/codex@latest`; the script reads version from `~/.codex/version.json` which `codex` updates on launch |
| `HTTP 401` / `HTTP 403` then refresh works | Token expired and refresh succeeded | No action needed — script auto-retried |
| `refresh_token is no longer valid — run codex login again` | Refresh token revoked or rotated | Tell user to run `codex login` again |
| `stalled: the image backend sent no data for ~Ns (last phase: …)` | No data for the whole `--stall-timeout` idle window — backend hung or overloaded | Retry; if it recurs, raise `--stall-timeout` (and `--timeout`), or set `--stall-timeout 0` to wait out the full `--timeout`. The message names the phase it stalled in. |
| `timed out: no image within the Ns total budget (last phase: …)` | The whole `--timeout` budget elapsed — usually a genuinely large image | Raise `--timeout` (e.g. `--timeout 420`) and retry |
| `timed out after Ns waiting to confirm the image … The prompt was already submitted` | (web) No image was confirmed before the original deadline; this does not prove the request failed | Do not re-prompt. The error names the conversation: run `image-use recover <that URL> -o PATH` (add `--quiet` for path-only stdout) once the image is there; "still working" means try again in a minute. Needs the `chatgpt/images` chrome-use adapter (`chrome-use site update`). `auto` does not fall back to Codex after submission. |
| `ChatGPT reported an image-generation limit` / `ChatGPT declined the image request` | (web) The non-streaming assistant reply starts with a recognized English quota/refusal message, with no fresh image present | Read the attached `ChatGPT said` text for the reason or reset time, and check the submitted conversation before retrying. Do not replay through another backend. |
| `no image returned. events seen: ...` | Model decided not to call the tool | Rephrase prompt to explicitly say "Use the image_generation tool to render…" |
| `HTTP 429` | Subscription rate-limited | Wait a few minutes; do not retry in a loop |
| `warning: --format=X but FILE.Y has .Y extension` | `-o` extension disagrees with `--format` | Fix the path or the format flag; the file IS written with the format you specified |
| `warning: project 'X' unavailable (…); using a plain chat` | (web, without `--require-project`) Project list/create API hiccup, or the project page's composer didn't render | Nothing — the image still generated, just in a top-level chat. If it recurs, check the target; use `--require-project` to stop instead of degrading |
| `required project unavailable` / `required Project Send stopped` | Project access, route, or composer state could not be verified | Stop; inspect the Project link, permissions, and retained draft. Manually clear any retained composer text before the next run; ChatGPT can restore drafts in new chats. No plain-chat or Codex fallback. An uncertain Send is never repeated; inspect the conversation before retrying. |
| `chatgpt.com rate-limited this account ('Too many requests') …` | (web) The page surface temporarily blocked the account for making requests too quickly | Wait a few minutes. A detected rate limit stops the run without automatic Codex fallback; if the prompt was already submitted, check the conversation later — the image may still appear there. Don't retry in a loop |
| `waiting for a free web/codex slot (max N concurrent …)` | More parallel runs than the backend's concurrency cap | Nothing — the run starts when a slot frees up; queue time doesn't eat `--timeout` |

## Upgrade

When any `image-use` command prints `image-use X is available`, tell the user and offer to run
`image-use upgrade` (it updates the CLI and this skill). Check without changing anything:
`image-use upgrade --check` (or `--json`). The user may also just say "升级 image-use" / "upgrade image-use".
`image-use update` and the old `chatgpt-imagegen update` do the same thing.

If the skill came from somewhere `upgrade` can't refresh:
- Claude Code plugin: `claude plugin update image-use@leeguooooo-plugins`
- Whole family: `curl -fsSL https://raw.githubusercontent.com/leeguooooo/plugins/main/upgrade-use-family.sh | sh`

The notice is one stderr line, checked at most once a day; `IMAGE_USE_NO_UPDATE_CHECK=1` (or the old `CHATGPT_IMAGEGEN_NO_UPDATE_CHECK`, or the family-wide `USE_NO_UPDATE_CHECK`) turns it off.

## Internals (for maintainers / debugging)

**web backend (`run_web`)**
- Shells out to `chrome-use` against a session-named Chrome tab group.
- Opens a *regular* `https://chatgpt.com/` chat (Temporary Chat disables the image tool).
- Resolves a named ChatGPT Project from inside the authenticated page (undocumented endpoints, probed live): `GET /backend-api/gizmos/snorlax/sidebar` lists projects (a project is a gizmo with id `g-p-…`); `POST /backend-api/projects {name, instructions}` creates one. An existing Project URL bypasses both endpoints and opens `https://chatgpt.com/g/<g-p-id>/project` directly. Failures warn and degrade to a plain chat by default; `--require-project` makes them fatal and prevents auto-mode Codex fallback.
- Pastes with `fill --stdin`, verifies the editor's exact logical text and every completed reference upload, then clicks Send once. Required mode also checks the Project before reference upload/paste. It marks only the current document's Send button; a capture listener rechecks route, text, uploads, turns, and readiness during its native click. The marker/listener are removed after the attempt, or expire after 30 seconds if the CLI disappears. Multiline `keyboard type` can turn newlines into partial submissions. Unconfirmed uploads or changed text stop the run; an uncertain send is observed without replaying it. Existing drafts are preserved. Pre-send failures clear the pasted text in legacy mode; required mode retains the current draft because route or ownership may have changed. Inspect it and manually clear the composer before retrying.
- Polls page state via `eval`: waits until the streaming/stop control is gone AND a brand-new `<img>` (src matching `estuary/content|files/download|oaiusercontent`) is present and stable across two reads. The img scan is scoped to `main img` (the tab's own conversation thread) — ChatGPT pushes an "Image created" toast with a matching thumbnail into any open tab when *another* conversation finishes an image, and a document-wide scan grabs that sibling's image (issue #7). The generated img is NOT inside `[data-message-author-role="assistant"]`, so `<main>` is the right scope.
- Downloads the bytes with an in-page `fetch(src, {credentials:'include'})` → base64, so the browser's own session cookies authorize the signed asset URL. No tokens leave the browser.

**codex backend (`run_codex`)**
- Reads `~/.codex/auth.json` for `access_token`, `account_id`, `refresh_token`; reads `~/.codex/version.json` for the `version` header.
- POSTs to `https://chatgpt.com/backend-api/codex/responses` with `tools: [{"type": "image_generation"}]` (plus any of `model`/`quality`/`background`/`output_compression`/`action`/`partial_images` the user opted into — unset knobs are omitted), streams the SSE response, base64-decodes the `image_generation_call` result. The server rewrites the tool config and reports the effective one in `response.created`; the CLI captures it (plus `response.completed.usage`) to print the real `model=` and `tokens=`. The default driver is `gpt-5.6-luna`; a model outside the account allowlist (`GET /backend-api/codex/models?client_version=…`) falls back to `gpt-5.5` rather than failing the run.
- Auto-refreshes the OAuth token on 401/403 via `https://auth.openai.com/oauth/token` (`client_id=app_EMoamEEZ73f0CkXaXp7hrann`); the refreshed token is persisted back to `auth.json`.

Why the web surface is reachable only through a real browser: the consumer `backend-api/*` paths are gated by three layers — Cloudflare's edge check, a sentinel proof-of-work (`sentinel/chat-requirements` + an in-page `sentinel/sdk.js` that computes the token), and a **Cloudflare Turnstile** token. Tested empirically: a bare bearer-token request from a residential IP **passes** the Cloudflare edge and the PoW (CF is IP-reputation-based; the PoW is hashcash-style and replicable offline) — the actual wall is **Turnstile**, an interactive token a headless client can't forge. And "borrow a browser only for the Turnstile token, then go headless" is self-defeating: the token is single-use and short-lived, so you'd open a browser every request anyway. That's why the web backend drives a genuine logged-in browser; the only true no-browser path is the `codex` backend (which bills Codex-usage).

## Reporting issues — you are the feedback channel

This tool is used mostly *through other people's agents*, so **maintainers rarely see failures directly — the agent running it is the main way bugs surface.** When you hit something that looks like a tool bug, help get it reported instead of silently working around it.

- **What's worth reporting:** a crash/traceback, a backend that *should* work but doesn't, wrong or garbled output, a misleading/wrong error message, a `web`/`chrome-use` failure that `image-use doctor` says should be fine, or behavior that contradicts this SKILL.
- **What's NOT a bug (handle, don't file):** the user simply isn't logged in or hasn't installed a backend (setup — point them at `doctor` + the install steps), or a deliberate content refusal by the model.
- **How:** offer to file it — or hand the user a ready-to-paste report — at **https://github.com/leeguooooo/image-use/issues**. Search open issues first to avoid duplicates. Include:
  - the exact command you ran,
  - `image-use -V` (version),
  - the full error text / unexpected output,
  - the output of `image-use doctor`.
- A 30-second issue with a repro is worth far more than a quiet workaround — it's how this tool gets fixed.

## Related

- HTTP gateway sibling (for multi-app / SDK-compatible usage): https://github.com/leeguooooo/agent-cli-to-api
