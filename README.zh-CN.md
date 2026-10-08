# image-use

[![CI](https://github.com/leeguooooo/image-use/actions/workflows/ci.yml/badge.svg)](https://github.com/leeguooooo/image-use/actions/workflows/ci.yml)

[English](./README.md) | **中文**

**用你已有的订阅生图 —— 不需要 `OPENAI_API_KEY`。**

一个零依赖的单文件 Python 命令行工具(也是 AI-agent skill),纯 stdlib。默认走你的 ChatGPT 订阅,不可用时退到 Codex,有 Gemini 订阅也可以改用 Gemini。**免费 ChatGPT 账号也能用**——默认后端就是驱动普通的 ChatGPT 网页对话,免费档也能生图。

> **原名 `chatgpt-imagegen`。** 后端不止 ChatGPT 之后改了名。`chatgpt-imagegen` 命令仍可用(兼容别名),所有 `CHATGPT_IMAGEGEN_*` 环境变量照常生效(同时设了新名 `IMAGE_USE_*` 时以新名为准)。已保存的风格不受影响。

```bash
image-use "一只坐在窗台的水彩橘猫" -o cat.png
# -> saved: cat.png  (812,344 bytes)  size=1024x1024  quality=medium
```

<img width="1494" height="870" alt="image" src="https://github.com/user-attachments/assets/b48b0563-58a3-41ff-a207-f01eafbf2ccb" />

---

## 安装

需要 Python 3.10+ 和一个 ChatGPT 订阅(免费档也行)。

**给 AI agent 用(推荐)**——把 skill 装进 Claude Code、Codex、Cursor 等:

```bash
npx skills add leeguooooo/image-use -g
```

然后直接说:*"画一张 …"*。

**独立命令行**——不用 `pip`、不用虚拟环境:

```bash
git clone https://github.com/leeguooooo/image-use
sudo install image-use/image-use /usr/local/bin/image-use
sudo ln -sf image-use /usr/local/bin/chatgpt-imagegen   # 可选:保留旧命令名
```

还需要**一个后端**——`web`(默认,驱动你登录着的 Chrome,不花 Codex 用量)或 `codex`(无头兜底)。`image-use doctor` 看哪个就绪。→ **[后端与排错](https://drawstyle.leeguoo.com/zh/docs/backends)**

也有 **Gemini** 订阅?还有两个走 Google 账号的后端:`--backend gemini`(驱动登录着 `gemini.google.com` 的 Chrome)和 `--backend agy`(Antigravity CLI,无头)。两者**额度互相独立**,一个用完另一个还能顶上。它们都不会被自动选中,得点名使用。用 `--gemini-profile` 指定有订阅的那个 Chrome profile——因为几乎每个 profile 都登录着*某个* Google 账号,自动探测分不出来。另外注意:Gemini 的**文生图**结果右下角带可见水印(图生图没有);`--size` 在这条路上控制的是画面比例,不是精确像素数。

## 升级

```bash
image-use upgrade            # 装最新版并刷新 skill
image-use upgrade --check    # 只查不改:image-use 0.29.2 -> 0.30.0
image-use upgrade --json     # 同上,输出 JSON
```

`upgrade`(别名 `update`)按这份安装的来路装最新的 GitHub Release——`npx skills add` 装的走 `skills update`,git clone 的走 `git pull --ff-only`,单独拷贝的脚本直接换成新版——然后刷新它找到的其他 skill 副本(Claude Code 插件,以及 `~/.agents/skills`、`~/.claude/skills`、`~/.codex/skills` 下的 clone 和拷贝)。其他命令每天最多检查一次新版,有新版就往 stderr 打一行提示。设 `IMAGE_USE_NO_UPDATE_CHECK=1` 或全家通用的 `USE_NO_UPDATE_CHECK=1` 可关闭检查,设了 `CI` 时也不检查。不运行 `upgrade` 就不会安装任何东西。

**还停在 0.23.1 或更早?** 那时的自升级只找全局 `skills`,找不到就放弃,所以它没法把这个修复本身装进来。先手动破一次局(改名前的安装在 skills 里登记的名字是 `chatgpt-imagegen`):

```bash
npx -y skills update chatgpt-imagegen
```

之后 `image-use upgrade` 就能自己跑了。

## 用法

```bash
image-use "阴郁的山间日落" -o web/hero.png --size 1536x1024
image-use "改成暖调黄昏、电影感 35mm" -i photo.jpg          # 以参考图为主体
image-use "换成另一个虚构的人" --composition-ref photo.jpg  # 只借构图,不复刻人脸
image-use "一个机器人吉祥物" --style doodle                  # 套用画廊风格(本地没有会自动拉取并保存)
image-use animate "小狗开心地摇尾巴" --style-online snoopy --also-gif
OUT=$(image-use "icon" --quiet)                             # 只拿路径(便于管道)
image-use "产品主图" --backend codex --image-model gpt-image-2.5-sunburst --quality xhigh
```

最后一行是 GPT Image 2.5 的出图参数——`--image-model`(`sunburst` 精修 /`flare` 快而高质量)、`--quality`(`low`→`max`)、`--background transparent`、`--compression`、`--action`、`--partial-images`。这些**只对 codex 后端生效**(web/gemini 没这些控件),全部可选,且都是**请求**而非保证——保存时那行会打印后端实际用的 `model=`/`quality=`/`size=`,以它为准。

下面三张都是上面这些命令直接出的图,没有后期修饰:

<table>
<tr>
<td width="33%"><img src="./docs/gallery/watercolor-cat.png" alt="窗台上的水彩猫"></td>
<td width="33%"><img src="./docs/gallery/mountain-sunset.png" alt="阴郁的山间日落"></td>
<td width="33%"><img src="./docs/gallery/coffee-logo.png" alt="咖啡店 logo"></td>
</tr>
<tr>
<td><sub><code>"一只坐在窗台的水彩橘猫"</code></sub></td>
<td><sub><code>"阴郁的山间日落" --size 1536x1024</code></sub></td>
<td><sub><code>"一个咖啡店 logo,圆形徽章"</code></sub></td>
</tr>
</table>

`animate` 会让模型生成严格的 4×2 雪碧图,等分裁出 8 帧,检测明显的主体漂移,
再编码成平滑的往返循环。默认输出动态 WebP;需要 GIF 时使用
`--animation-format gif` 或 `--also-gif`。原始雪碧图 PNG 会始终保存在动图旁边。
动画后处理需要 [ImageMagick](https://imagemagick.org/)(`magick`);输出 WebP
还需要 [libwebp](https://developers.google.com/speed/webp/download)(`img2webp`)。
`image-use doctor` 会检查两者是否就绪。

完整参数:`image-use --help`。→ **[生成图片](https://drawstyle.leeguoo.com/zh/docs/generate)** · **[风格系统](https://drawstyle.leeguoo.com/zh/docs/styles)**

web 后端的 `--project` 可以传精确**名称**(找到就复用,没有就创建;默认 `imagegen`),
也可以传已有 **Project 首页链接**,形如
`https://chatgpt.com/g/g-p-<32-hex-id>/project`。链接按完整 ID 直接打开,不依赖侧栏列表,
也不创建 Project;支持中文等显示名 slug、query 和 fragment,打开时去掉这些装饰。
格式错误的链接会直接报错。`--project ""` 则使用普通 chat。

默认仍是 best-effort:Project 不可用时会警告并继续使用普通 chat。如果必须在目标
Project 中提交,加 `--require-project`(或 `IMAGE_USE_REQUIRE_PROJECT=1`)。
它要求目标非空,backend 为 `web` 或 `auto`,进入后核对 Project 身份,并在原生
发送点击事件的捕获阶段再次校验身份和输入状态;浏览器不可用时也禁止回退 Codex。
`IMAGE_USE_PROJECT` 同样可以设置名称或链接。
用 `--no-require-project` 可以只在本次运行覆盖环境变量的默认值,恢复 best-effort
路由和普通 backend/fallback 规则,不需要修改已 export 的变量。

required 模式在发送前失败时会保留当前草稿供检查。请先在 ChatGPT 中检查草稿,
再手动清空输入框后运行下一次;ChatGPT 可能会在新对话里恢复未发送的草稿。

```bash
image-use "一只水彩猫" --project "Art" --require-project --keep-conversation
# 精确指定已有 Project 时,先把浏览器里的首页链接赋给 PROJECT_URL:
image-use "一只水彩猫" --project "$PROJECT_URL" --require-project --keep-conversation
```

路由与保留对话是独立选项:**默认仍会删除对话**,即使加了 `--require-project`。
要让对话留在 Project 中,加 `--keep-conversation`(或 `IMAGE_USE_KEEP_CONVERSATION=1`);
`--keep-tab` 也会保留对话。

ChatGPT 浏览器后端会粘贴多行提示词,核对编辑器中的完整文本,等待所有参考图上传
完成后只点击一次发送。上传不完整或文本发生变化时会停止;无法确认发送结果时会
报告问题,不会重复发送。运行前请确保输入框为空,已有草稿会被保留。未发送就中止的运行
会清掉自己粘贴的文本;required Project 模式会保留当前草稿供检查,避免在路由或草稿
归属变化后误清其他内容。

发送后,web 后端会在 `--timeout` 内等待新图片,要求相邻两次页面读取确认到同一张图。
assistant 正文出现或 Stop 控件缺失,都不能证明图片任务已经结束。已检测到的限流
弹窗会立即停止;没有图片和 streaming 控件时,以明确英文额度或拒绝提示开头的正文
(例如 "You've hit the image generation limit"、"I can't generate that image.")
也会立即报错。引用中的提示、含糊的 "try again later" 和其他未识别正文仍等待总期限;
这不是覆盖所有措辞和语言的额度/拒绝检测器。正文识别错误和超时会附上 assistant
正文(最多 240 字符);超时会保留读取中断前最后一段非空正文。超时后先检查原会话再重试:
图片仍可能在那里出现,`auto` 也不会在提交后自动改用 Codex。报错里会给出会话链接,
等图出来后用下面的命令直接下载,不用重新提交:

```bash
image-use recover https://chatgpt.com/c/<id> -o out.png
```

`recover` 依赖 [chrome-use-sites](https://github.com/leeguooooo/chrome-use-sites) 里的
`chatgpt/images` adapter(`chrome-use site update` 会装上),和 web 生图共用同一个
chatgpt.com 标签页和锁。

## 社区风格

浏览、复用别人调好的画风——公共画廊在 **[drawstyle.leeguoo.com](https://drawstyle.leeguoo.com)**,不用更新脚本:

```bash
image-use "一只狐狸咖啡师" --style-online doodle  # 直接用画廊风格生图,本地不落盘
image-use style search "水彩 吉祥物"              # 搜索画廊
image-use style publish mystyle --category cute --from-last   # 分享你的(需一次登录)
```

风格不只能固定画风,还能**固定角色**。风格资产可以绑参考图,同一个角色能在全新场景里复现:

```bash
image-use style add pip --kind character --ref pip-ref.png
image-use "一只狐狸咖啡师" --style pip
```

<table>
<tr>
<td width="50%"><img src="./docs/gallery/pip-ref.png" alt="小狐狸 Pip —— 角色参考图"></td>
<td width="50%"><img src="./docs/gallery/pip-cafe.png" alt="小狐狸 Pip 在咖啡馆场景中重绘"></td>
</tr>
<tr>
<td align="center"><sub>绑定的参考图</sub></td>
<td align="center"><sub>用 <code>"一只狐狸咖啡师"</code> 生成</sub></td>
</tr>
</table>

画廊里的包也可以是角色包——`xiaohei` 就是。

→ **[用画廊的风格](https://drawstyle.leeguoo.com/zh/docs/community)** · **[投稿与审核](https://drawstyle.leeguoo.com/zh/docs/submit)**

## 了解更多

- 📖 **[完整文档](https://drawstyle.leeguoo.com/zh/docs)** —— 安装、生图、风格、后端、平台。
- 🎨 **[画风画廊](https://drawstyle.leeguoo.com)** —— 浏览与投稿社区画风。
- 📝 **[博客深入](https://blog.leeguoo.com/zh/posts/chatgpt-imagegen/)** —— 背后的设计与原理。
- ⚙️ **[工作原理](./docs/how-it-works.zh-CN.md)** · **[HTTP API 封装](https://github.com/leeguooooo/agent-cli-to-api)**

## 许可

MIT —— 见 [LICENSE](./LICENSE)。

## 免责声明

本工具调用 ChatGPT 内部的 `backend-api/codex` 接口——和官方 Codex CLI 用的是同一个。它不是有文档的公开 API,OpenAI 随时可能更改或限制。请自担风险,并遵守 [OpenAI 使用条款](https://openai.com/policies/row-terms-of-use/)——尤其**不要用你的 ChatGPT 订阅去支撑一个对外公开的生图服务**。

<details>
<summary>关键词</summary>

用 ChatGPT 订阅生成图片、免费 ChatGPT 账号生图、ChatGPT Plus 生图工具、不用 API key 生图、gpt-image-2.5 用订阅、gpt-image-2 用订阅、ChatGPT 订阅生图 CLI、Codex CLI 生图能力独立工具、给 AI agent 用的生图 skill、本地生图脚本、零依赖 Python 生图工具。
</details>

## 作者

**郭立（Guo Li / leeguoo）** 开发 —— [leeguoo.com](https://leeguoo.com/about) · [GitHub](https://github.com/leeguooooo) · [X](https://x.com/leeguooooo) · 更多工具见 [*-use 家族](https://github.com/leeguooooo/plugins)。
