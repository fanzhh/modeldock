# ModelDock

把 Colab 变成你的按需 GPU 工作台：连接你自己的 **Google Drive** 和 **Colab**，
把大模型囤在云盘，需要时一键部署到 Colab GPU，用完自动释放。
开源、本地优先，凭证不出本机。

> 设计文档：[DESIGN.md](./DESIGN.md) · 界面原型：[design/prototype.html](./design/prototype.html)

## 当前状态（M2：Colab 编排完成）

- ✅ 设计稿 + 高保真可交互原型（5 页面 + Chat）
- ✅ `server/`：FastAPI + SQLite 元数据层
  - 凭证保险箱（Fernet 加密，密钥 0600，token 只写不读）
  - Google Drive rclone 通道封装（不走 Colab drive.mount，见 DESIGN.md §3）
  - Google OAuth 向导 + 已有 rclone remote 一键导入（托管 `gdrive`，0600）
  - 模型下载引擎：HF / 魔搭双源解析 + 流式断点续传 + 队列
  - Colab 编排：backend 抽象（mock / google-colab-cli）、
    provision→install→sync→boot→ready 状态机、GPU 降级链、
    临时 rclone 凭证随会话销毁、notebook 手动兜底、compute units 余额
  - API：accounts / models(families,tasks) / recipes / deployments(start/cancel/release/logs/notebook)
- ✅ `web/`：Vite React 开发控制台（拉取 health / 凭证 / 家族 / 任务）
- 🚧 内置配方（llama.cpp / ComfyUI 预置）、Chat 面板、配方便签化

## 快速开始

依赖：Python 3.11+（建议 [uv](https://docs.astral.sh/uv/)）、Node 18+、rclone v1.60+
可选：google-colab-cli（真实 Colab 通道）

```bash
# 1. 后端（默认监听 127.0.0.1:8820，数据目录 ~/.modeldock）
cd server
uv sync
uv run pytest                 # 39 个测试
uv run python -m uvicorn app.main:app --port 8820

# 2. 前端开发台（另开终端；代理 /api → 127.0.0.1:8820）
cd web
npm install
npm run dev                   # http://127.0.0.1:5173
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

## M1：连接 Google Drive（两条路）

先看状态（自动探测本机 `~/.config/rclone/rclone.conf` 里已有的 drive remote）：

```bash
curl localhost:8820/api/accounts/drive/status
```

**路 A — 导入已有 rclone remote**（本机已在用 rclone 的话最快）：

```bash
curl -X POST localhost:8820/api/accounts/drive/import \
  -H 'Content-Type: application/json' \
  -d '{"remote_name":"gdrive"}'     # 复制进托管配置 ~/.modeldock/rclone.conf（0600）
```

**路 B — OAuth 向导**：需先在 Google Cloud Console 建 OAuth 客户端
（类型「桌面应用」），重定向 URI 填 `http://127.0.0.1:8820/api/accounts/drive/oauth/callback`：

```bash
# 1. 发起（client_secret 即时入 vault，不回显）
curl -X POST localhost:8820/api/accounts/drive/oauth/start \
  -H 'Content-Type: application/json' \
  -d '{"client_id":"xxx.apps.googleusercontent.com","client_secret":"GOCSPX-xxx"}'
# 2. 浏览器打开返回的 authorize_url 授权；回调自动完成
#    token 交换 → vault 密封 → 注册托管 remote `gdrive`
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
│   └── tests/              39 个单元/集成测试
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

## 路线图（对应 DESIGN.md §8）

- [x] M1：Google Drive 接入（状态探测 / 已有 remote 一键导入 / OAuth 向导，托管 remote `gdrive`）
- [x] M2：Colab 会话编排（create→install→sync→run→health→release + GPU 降级 + notebook 兜底）
- [ ] M3：内置配方（ComfyUI H3 / llama.cpp chat）+ Chat 面板
- [ ] M4：余额快照、自动释放策略、模型家族管理 UI

## 安全原则

1. token/refresh_token 只存本机加密 vault（Fernet，密钥 0600）
2. DB 只存指纹；API 只写不读；日志脱敏
3. 服务默认只监听 127.0.0.1
4. 模型下载只做编排分发，不内置盗版源
