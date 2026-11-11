# ModelDock

把 Colab 变成你的按需 GPU 工作台：连接你自己的 **Google Drive** 和 **Colab**，
把大模型囤在云盘，需要时一键部署到 Colab GPU，用完自动释放。
开源、本地优先，凭证不出本机。

> 设计文档：[DESIGN.md](./DESIGN.md) · 界面原型：[design/prototype.html](./design/prototype.html)

## 当前状态（M4：控制台 + 生命周期治理完成）

- ✅ 设计稿 + 高保真可交互原型（5 页面 + Chat）
- ✅ `server/`：FastAPI + SQLite 元数据层
  - 凭证保险箱（Fernet 加密，密钥 0600，token 只写不读）
  - Google Drive rclone 通道封装（不走 Colab drive.mount，见 DESIGN.md §3）
  - Google OAuth 向导 + 已有 rclone remote 一键导入（托管 `gdrive`，0600）
  - 模型下载引擎：HF / 魔搭双源解析 + 流式断点续传 + 队列
  - Colab 编排：backend 抽象（mock / google-colab-cli）、
    provision→install→sync→boot→ready 状态机、GPU 降级链、
    临时 rclone 凭证随会话销毁、notebook 手动兜底、compute units 余额
  - 内置配方：`builtin/llamacpp-chat`（llama.cpp GGUF 聊天）+ `builtin/comfyui`；
    `$family` 模板由 deployment params 解析
  - Chat：线程/历史本地保存、OpenAI 兼容转发（非流式 + SSE 流式透传）、
    history_limit 截断、端点登记
  - 余额快照：启动/释放各抓一次 usage → params.usage_before/after；
    空闲自动释放（Chat 活动或 ready 时间为基准，超时自动 release）
  - API：accounts / models / recipes / deployments / chat / colab-usage
- ✅ `web/`：React 控制台（总览 / 模型仓库 / 部署 / Chat 四页签，
  token/家族/任务录入、部署状态与日志/释放/兜底导出、SSE 流式对话）；
  `npm run build` 后由 server 一体化托管
- 🚧 vLLM 配方、多 GPU 预检、配方便签化

## 快速开始

依赖：Python 3.11+（建议 [uv](https://docs.astral.sh/uv/)）、Node 18+、rclone v1.60+
可选：google-colab-cli（真实 Colab 通道）

```bash
# 1. 后端（默认监听 127.0.0.1:8820，数据目录 ~/.modeldock）
cd server
uv sync
uv run pytest                 # 77 个测试
uv run python -m uvicorn app.main:app --port 8820

# 2. 前端控制台（开发调试）
cd web
npm install
npm run dev                   # http://127.0.0.1:5173（代理 /api）

# 生产形态：构建后由 server 直接托管，单进程一体化
cd web && npm run build       # 产物 web/dist → uvicorn 启动后直接开 http://127.0.0.1:8820/
```

### 试一下 API

```bash
# 存 HuggingFace token（响应不含明文，只有指纹）
curl -X PUT localhost:8820/api/accounts/secrets -H 'Content-Type: application/json' \
  -d '{"scope":"hf","name":"main","secret":"hf_xxx"}'

# 建模型家族 + 排队下载任务（魔搭源）
curl -X POST localhost:8820/api/models/families -H 'Content-Type: application/json' \
  -d '{"name":"Qwen3-8B","purpose":"chat"}'
curl -X POST localhost:8820/api/models/tasks -H 'Content-Type: application/json' \
  -d '{"family_id":1,"source":"modelscope","repo":"Qwen/Qwen3-8B-GGUF","filename":"q3-q4.gguf","target_remote_path":"qwen3/q3-q4.gguf"}'

curl localhost:8820/api/health
```

## M1：连接 Google Drive（网页上即可完成）

打开控制台「总览」页：检测到本机已有 rclone remote 时点**一键导入**；
没有则在 Drive 卡片的 OAuth 向导里填 Google Cloud Console 的 client_id/secret，
按页面给出的 redirect_uri 到 Console 注册（Web 应用需 HTTPS；loopback 例外），
打开授权链接批准即可——回调随浏览器访问地址动态生成，
本机（127.0.0.1）和服务器部署（Tailscale 域名）都支持。

服务器上建议 `tailscale serve --bg 8820` 获得 HTTPS，如
`https://<host>.<tailnet>.ts.net/api/accounts/drive/oauth/callback`。

「登录 Colab」同样在总览页点按钮完成：网页展示授权链接 → 浏览器批准 →
授权码粘回网页（凭证由 colab CLI 自管，授权码只在内存转交）。
**colab CLI 随 server 依赖自带**（`google-colab-cli`），部署者无需单独安装。

部署就绪后到「部署」页点**开隧道**：服务端自动生成 SSH 密钥与配置、
经 `colab ssh --proxy-mode` 建立端口转发，并把本地 endpoint 自动登记——
「Chat」页与任何 OpenAI 兼容客户端即可直接使用，全程无终端操作。

以下为等价的 API 操作：

```bash
curl localhost:8820/api/accounts/drive/status
curl -X POST localhost:8820/api/accounts/drive/import \
  -H 'Content-Type: application/json' -d '{"remote_name":"gdrive"}'
curl -X POST localhost:8820/api/accounts/drive/oauth/start \
  -H 'Content-Type: application/json' \
  -d '{"client_id":"xxx.apps.googleusercontent.com","client_secret":"GOCSPX-xxx"}'
# 浏览器打开返回的 authorize_url 授权；回调自动完成

curl -X POST localhost:8820/api/colab/auth/start    # Colab 登录：授权链接
curl -X POST localhost:8820/api/colab/auth/submit -H 'Content-Type: application/json' \
  -d '{"code":"4/0A…"}'
curl -X POST localhost:8820/api/deployments/1/tunnel/start   # 一键隧道+端点登记
curl localhost:8820/api/deployments/1/tunnel/status
```

连好后检查健康与配额：

```bash
curl localhost:8820/api/accounts/drive/quota   # 409 = 未注册
```

> Drive 权限只申请 `https://www.googleapis.com/auth/drive`；
> refresh_token 明文只存在于 0600 的 rclone 配置与加密 vault 中。

## M2：部署到 Colab

准备：连接 Drive（上面 M1）+ 安装 Colab CLI 并登录（`uv tool install google-colab-cli`
后运行一次 `colab new` 完成浏览器授权），然后切到真实后端：

```bash
MODELDOCK_COLAB_BACKEND=cli uv run python -m uvicorn app.main:app --port 8820
```

一键部署（这里以内置的 llama.cpp 聊天配方为例）：

```bash
# 建配方（models.role.family 引用已入库的模型家族；ports 声明服务端口）
curl -X POST localhost:8820/api/recipes -H 'Content-Type: application/json' -d '{
  "name":"llamacpp-chat","kind_class":"chat",
  "manifest":{"models":{"chat":{"family":"Qwen3-8B"}},"ports":[8080]},
  "install_script":"git clone --depth 1 https://github.com/ggerganov/llama.cpp && cd llama.cpp && make -j server",
  "run_script":"./llama-server -m /content/models/Qwen3-8B/q3-q4.gguf --host 0.0.0.0 --port 8080",
  "ports":[8080]}'

# 建部署并启动流水线（A100 不可用时自动 L4→T4 降级）
curl -X POST localhost:8820/api/deployments -H 'Content-Type: application/json' -d '{"recipe_id":1,"gpu":"A100"}'
curl -X POST localhost:8820/api/deployments/1/start

curl localhost:8820/api/deployments/1/logs        # 逐步日志
curl localhost:8820/api/colab/usage               # compute units 余额
curl -X POST localhost:8820/api/deployments/1/release   # 用完释放（删临时凭证→停 VM）
```

编排状态机：`queued → provision → install → syncing_models → booting → ready →
(releasing → done)`，任一步 `failed/cancelled`。模型装配在 Colab 内用 rclone 从
Drive 拉取（不走 drive.mount）；临时 rclone 配置只写 `/content/.secrets`，释放即删。

**兜底**：Colab API 变动导致自动编排失效时，导出等价的手动单元格，
直接粘贴到 Colab 网页执行：

```bash
curl localhost:8820/api/deployments/1/notebook
```

## M3：内置配方 + Chat

启动时自动 seed 两个内置配方（改过不会被覆盖）：

| 配方 | 用途 | 说明 |
|---|---|---|
| `builtin/llamacpp-chat` | 聊天 | llama.cpp llama-server（CUDA 构建），任意 GGUF；OpenAI 兼容 `/v1` |
| `builtin/comfyui` | 图像/视频 | ComfyUI + GGUF/视频节点，Web UI 8188 |

内置配方的模型角色是模板（`"$chat"`、`"$checkpoint"`），部署创建时由
`params.families` 提供真实家族名：

```bash
# 用内置聊天配方部署 Qwen3-8B GGUF（families.chat → 已入库家族名）
curl -X POST localhost:8820/api/deployments -H 'Content-Type: application/json' -d '{
  "recipe_id": 1, "gpu": "T4",
  "params": {"families": {"chat": "Qwen3-8B"}}}'
curl -X POST localhost:8820/api/deployments/1/start
```

**Chat 面板（API）**：部署 ready 后，把 Colab 端口（SSH 隧道 `colab ssh`，
或能直连的 URL）映射成本地地址登记，然后开线程对话：

```bash
# 登记本地转发端点（llama-server 在 Colab :8080 → 本机 127.0.0.1:18080）
curl -X PATCH localhost:8820/api/chat/deployments/1/endpoint \
  -H 'Content-Type: application/json' -d '{"endpoint":"http://127.0.0.1:18080"}'

# 开线程 + 对话（历史、system prompt、temperature/top_p/max_tokens 由 thread.params 控制）
curl -X POST localhost:8820/api/chat/deployments/1/threads \
  -H 'Content-Type: application/json' \
  -d '{"deployment_id":1,"params":{"system_prompt":"You are terse.","temperature":0.5}}'
curl -X POST localhost:8820/api/chat/threads/1/messages \
  -H 'Content-Type: application/json' -d '{"message":"你好"}'
curl -X POST localhost:8820/api/chat/threads/1/messages/stream \
  -H 'Content-Type: application/json' -d '{"message":"继续"}'   # SSE 流式
curl localhost:8820/api/chat/threads/1                        # 回放历史
```

导出 OpenAI 兼容端点给本机其它应用（Cherry Studio 等）：登记的 endpoint
`http://127.0.0.1:18080/v1` 即可直接填入。

## 生成：图片与视频（「生成」页签）

部署 `builtin/comfyui` 并装配 checkpoint 家族（SD/SDXL/Flux safetensors 或
GGUF 工作流模型），ready 后点「开隧道」，到「生成」页：

- **文生图（开箱即用）**：选 checkpoint → 写提示词/负面/尺寸/步数/种子 →
  生成。产物进 gallery，可回看/删除。
- **生视频/进阶（工作流模式）**：在 ComfyUI 网页搭好 WAN 等工作流 →
  「导出（API Format）」→ 粘贴到文本框，支持 `{{prompt}} {{negative}} {{ckpt}}
  {{seed}} {{width}} {{height}} {{steps}}` 变量替换（裸写为 JSON 数值）。
- 产物文件经 server 代理取回（`/api/gen/assets/{id}/file`），浏览器不直连
  Colab；图用 `<img>`、视频用 `<video>` 直接播放。

对应 API：`POST /api/gen/deployments/{id}/submit`（提交 workflow）、
`GET …/history/{prompt_id}`（轮询，done 时入库）、`GET/DELETE …/assets`、
`GET …/checkpoints`（ComfyUI object_info）。

## 目录

```
modeldock/
├── DESIGN.md               设计稿（架构/模块/数据模型/风险）
├── design/prototype.html   可交互 HTML 原型
├── server/                 FastAPI 后端（uv 管理）
│   ├── app/
│   │   ├── api/            accounts / models / recipes / deployments
│   │   ├── core/           vault（加密）/ gauth（OAuth）/ drive（rclone）/ colab（编排）/ downloads（HF+魔搭+队列）
│   │   ├── config.py       环境变量 MODELDOCK_*
│   │   ├── db.py           SQLAlchemy 模型
│   │   └── main.py         入口 + lifespan
│   └── tests/              77 个单元/集成测试
├── web/                    Vite React 控制台骨架
└── docker-compose.yml      本地编排（含 rclone 的 server 容器）
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MODELDOCK_DATA_DIR` | `~/.modeldock` | SQLite / vault / rclone.conf / 下载缓存 |
| `MODELDOCK_HOST/PORT` | `127.0.0.1:8820` | 只监听本机；`ALLOW_REMOTE=1` 解锁并打警告 |
| `MODELDOCK_DRIVE_PREFIX` | `modeldock/models` | 云盘模型根目录 |
| `MODELDOCK_HF_ENDPOINT` | huggingface.co | 可换镜像 |
| `MODELDOCK_MAX_PARALLEL_DOWNLOADS` | 2 | 并发下载数 |

| `MODELDOCK_COLAB_BACKEND` | `mock` | Colab 后端：`mock`（dry-run）/ `cli`（真实会话） |
| `MODELDOCK_COLAB_BIN` | `~/.local/bin/colab` | colab CLI 路径（cli 后端） |
| `MODELDOCK_IDLE_TIMEOUT_MIN` | `0` | 全局空闲自动释放阈值（分钟，部署 params.idle_timeout_min 优先；0=关闭） |

## 路线图（对应 DESIGN.md §8）

- [x] M1：Google Drive 接入（状态探测 / 已有 remote 一键导入 / OAuth 向导，托管 remote `gdrive`）
- [x] M2：Colab 会话编排（create→install→sync→run→health→release + GPU 降级 + notebook 兜底）
- [x] M3：内置配方（llama.cpp chat / ComfyUI）+ Chat 历史/流式 API + $family 模板
- [x] M4：余额快照（usage before/after）、空闲自动释放、Web 控制台（总览/模型/部署/Chat 四页签，SSE 流式）
- [ ] v0.2+：vLLM 配方、多 GPU 预检、模型家族管理增强、配方便签化

## 安全原则

1. token/refresh_token 只存本机加密 vault（Fernet，密钥 0600）
2. DB 只存指纹；API 只写不读；日志脱敏
3. 服务默认只监听 127.0.0.1
4. 模型下载只做编排分发，不内置盗版源
