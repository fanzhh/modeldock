# ModelDock

把 Colab 变成你的按需 GPU 工作台：连接你自己的 **Google Drive** 和 **Colab**，
把大模型囤在云盘，需要时一键部署到 Colab GPU，用完自动释放。
开源、本地优先，凭证不出本机。

> 设计文档：[DESIGN.md](./DESIGN.md) · 界面原型：[design/prototype.html](./design/prototype.html)

## 当前状态（M1：Google Drive 接入完成）

- ✅ 设计稿 + 高保真可交互原型（5 页面 + Chat）
- ✅ `server/`：FastAPI + SQLite 元数据层
  - 凭证保险箱（Fernet 加密，密钥 0600，token 只写不读）
  - Google Drive rclone 通道封装（不走 Colab drive.mount，见 DESIGN.md §3）
  - Google OAuth 向导 + 已有 rclone remote 一键导入（托管 `gdrive`，0600）
  - 模型下载引擎：HF / 魔搭双源解析 + 流式断点续传 + 队列
  - API：accounts / models(families,tasks) / recipes / deployments
- ✅ `web/`：Vite React 开发控制台（拉取 health / 凭证 / 家族 / 任务）
- 🚧 Colab 会话编排（下一步）、Chat 面板、配方便签化

## 快速开始

依赖：Python 3.11+（建议 [uv](https://docs.astral.sh/uv/)）、Node 18+、rclone v1.60+

```bash
# 1. 后端（默认监听 127.0.0.1:8820，数据目录 ~/.modeldock）
cd server
uv sync
uv run pytest                 # 26 个测试
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

## 目录

```
modeldock/
├── DESIGN.md               设计稿（架构/模块/数据模型/风险）
├── design/prototype.html   可交互 HTML 原型
├── server/                 FastAPI 后端（uv 管理）
│   ├── app/
│   │   ├── api/            accounts / models / recipes / deployments
│   │   ├── core/           vault（加密）/ gauth（OAuth）/ drive（rclone）/ downloads（HF+魔搭+队列）
│   │   ├── config.py       环境变量 MODELDOCK_*
│   │   ├── db.py           SQLAlchemy 模型
│   │   └── main.py         入口 + lifespan
│   └── tests/              26 个单元/集成测试
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

## 路线图（对应 DESIGN.md §8）

- [x] M1：Google Drive 接入（状态探测 / 已有 remote 一键导入 / OAuth 向导，托管 remote `gdrive`）
- [ ] M2：Colab 会话编排（create→install→sync→run→tunnel→release）
- [ ] M3：内置配方（ComfyUI H3 / llama.cpp chat）+ Chat 面板
- [ ] M4：余额快照、自动释放策略、模型家族管理 UI

## 安全原则

1. token/refresh_token 只存本机加密 vault（Fernet，密钥 0600）
2. DB 只存指纹；API 只写不读；日志脱敏
3. 服务默认只监听 127.0.0.1
4. 模型下载只做编排分发，不内置盗版源
