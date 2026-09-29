# image-use 工作原理(技术)

[← 返回 README](../README.zh-CN.md)

## 背景:OpenAI 给图像生成收费的两条路

OpenAI 的图像生成有两条完全独立的路:

| 路径 | 怎么收费 | 怎么用 |
| --- | --- | --- |
| **直连 API**（`/v1/images/generations`） | 按张计费,需要 `OPENAI_API_KEY` | curl / OpenAI SDK 等 |
| **ChatGPT 订阅**（Plus / Pro / Team） | 每月固定费 | ChatGPT 网页/桌面 app,或 Codex CLI 内置的 `image_gen` |

对不用 Codex CLI 的人来说,订阅这条路是隐形的。它跑在 ChatGPT 内部的 `backend-api/codex/responses` 接口上(作为 Responses-API 工具),用 `codex login` 写进 `~/.codex/auth.json` 的 OAuth token 鉴权。`image-use` 把这份能力搬到命令行、也给任意 AI agent 用 —— 并提供两个后端,分别命中你订阅里的不同部分。

<img src="./two-backends.svg" width="760" alt="image-use —— web 与 codex 后端流程">

## `web` 后端(默认)

经 [`chrome-use`](https://github.com/leeguooooo/chrome-use) 驱动你登录的浏览器,让出图跑在消费级 ChatGPT 界面上 —— 无头客户端够不着。防线分三层:Cloudflare 边缘和 sentinel 工作量证明(`backend-api/sentinel/chat-requirements` + 页面内 `sentinel/sdk.js` 算 token)裸客户端**都能过**,真正过不了的是 **Cloudflare Turnstile** token —— 这个交互式 token 只能由真浏览器现场产出,且单次有效,没有"取了 token 再 headless"的捷径。流程:

```
image-use --backend web
   │
   ├── chrome-use open https://chatgpt.com/      (普通对话 —— Temporary Chat 禁用了出图工具)
   ├── 解析 ChatGPT 项目 (--project)              (页面内 fetch:gizmos/snorlax/sidebar 列项目,
   │   并打开 chatgpt.com/g/<g-p-id>/project       没有就 POST /backend-api/projects 创建)
   ├── 用真实键盘输入打提示词                       (ProseMirror/React 输入框不认纯 DOM 的 `fill`)
   ├── 轮询页面:等流结束 且 新的 <img> 资源稳定
   └── 在页面内 fetch 资源字节 (credentials:'include') → base64 → 存盘
       (签名的 estuary/content URL 由浏览器自己的 cookie 授权;
        2026-09 改版后图片是 blob: URL,只有页面自己能读)
```

token 不出浏览器。每次出图落在 `imagegen` 项目里(自动创建),且**默认出图后删除该对话**不留历史(`--keep-conversation` 可保留);传 `--project ""` 可退回普通顶层对话。

## `codex` 后端

Codex CLI 内置的 `image_gen` 能力是一个原生 Responses-API 工具:

```jsonc
// Codex CLI 发往 chatgpt.com/backend-api/codex/responses 的请求:
{
  "model": "gpt-5.5",
  // 该 tool 可选带 GPT Image 2.5 参数——model(gpt-image-2.5-sunburst/flare)、
  // quality(low…xhigh/max)、background、output_compression、action、partial_images
  "tools": [{"type": "image_generation"}],
  "input": [{"role": "user", "content": [{"type":"input_text","text":"draw a cat"}]}],
  // ...
}
```

服务器回一条 SSE 流,其 `response.output_item.done` 事件携带 `item.type === "image_generation_call"` 负载,其中 `item.result` 是 base64 PNG。`image-use` 就是这么做的:

```
image-use
   │
   ├── 读 ~/.codex/auth.json     (OAuth access_token、account_id、refresh_token)
   ├── 读 ~/.codex/version.json  (codex CLI 版本 → 匹配服务器预期)
   │
   └── POST https://chatgpt.com/backend-api/codex/responses
       headers: Authorization、version、originator、session_id 等
       body:    tools: [image_generation]
       │
       └── SSE 流
           ├── response.image_generation_call.in_progress    → "queued"
           ├── response.image_generation_call.generating      → "generating"
           ├── response.image_generation_call.partial_image   → "receiving image (partial N)"
           ├── response.output_item.done  ← item.result = base64 PNG
           └── response.completed
```

OAuth token 过期时,脚本会经 `https://auth.openai.com/oauth/token`(用 `codex login` 已存的 refresh_token)自动刷新,并把新 token 写回 `~/.codex/auth.json`。

## 深入(博客)

- [让 Claude Code 自己画图 —— chatgpt-imagegen 的设计与原理](https://blog.leeguoo.com/en/posts/chatgpt-imagegen/)
- [为什么有这个项目 + OAuth/SSE 走读](https://blog.misonote.com/zh/posts/chatgpt-subscription-image-api/) · [可视化速览](https://blog.misonote.com/zh/posts/chatgpt-imagegen-visual-guide/)(中文;英/日文在 `/en/`、`/ja/` 下)
