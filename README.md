# Huddle 🪶

> Slack 风格的本地多 agent 早会平台。
> Python 调 OpenClaw CLI 让一群 agent 在共享上下文里"开会"，web UI 渲染成 Slack 三栏视图。

## 架构（v0.4）

```
┌─────────────────────────────────────────────────────────────┐
│ ~/.openclaw/standups/                                       │
│   ├── data/<YYYY-MM-DD>/                                    │
│   │   ├── events.jsonl   ← 真相源（append-only + flock）   │
│   │   └── .lock          ← fcntl advisory lock              │
│   ├── squads.json        ← squad 配置（atomic replace）      │
│   └── standup-<date>.md  ← 派生视图（每次写事件后 atomic 重建）│
└─────────────────────────────────────────────────────────────┘
                ▲                 ▲
                │ writes          │ reads
        ┌───────┴────────┐  ┌─────┴────────┐
        │ run_standup.py │  │  server.py   │  → web UI (vanilla JS)
        │ (chair + 5     │  │  (HTTP API)  │
        │  agents 开会)  │  │              │
        └────────────────┘  └──────────────┘
```

**真相源不是 markdown 而是 events.jsonl**。markdown 只是"渲染出来给人看"的派生产物，每次新事件追加后会被原子重写。`squads.json` 存 squad 配置，目前 meeting 还没有 squad 元数据，所以历史 meeting 暂时全部归到默认 `milk-eng`。

## 文件结构

```
/Volumes/DevDisk/symbol/huddle/
├── README.md              ← 本文件
├── huddle_store.py        ← JSONL event store（append/lock/projection/render）
├── run_standup.py         ← 早会脚本（chair 主持 + topic 分段 + 独立 session）
├── server.py              ← 本地 HTTP server（API + 静态文件）
├── migrate_md_to_jsonl.py ← 把旧 standup-*.md 一次性导入新事件流
└── web/
    ├── index.html
    ├── style.css          ← Slack 三栏布局 + responsive
    └── app.js             ← vanilla JS（无依赖）
```

## 第一次运行

```bash
cd /Volumes/DevDisk/symbol/huddle

# 1. 把旧的 v0.1/v0.2 markdown 纪要导入新 jsonl 事件流（幂等）
python3 migrate_md_to_jsonl.py

# 2. 启动 web 服务
python3 server.py
# 浏览器打开 http://127.0.0.1:7878

# 3. 跑一次新早会（也可以直接在 web 的 MEETINGS 点 "+"）
python3 run_standup.py
```

## 设计要点

### Source-of-truth = `events.jsonl`

每次发言、每个 topic 切换都是一个 JSON 事件：

```jsonl
{"id":"evt_…","ts":"…","kind":"meeting_started","date":"…","chair":"milk","members":[…]}
{"id":"evt_…","ts":"…","kind":"topic_started","topic_id":"t1_…","idx":1,"title":"昨日进度","topic_kind":"topic"}
{"id":"evt_…","ts":"…","kind":"post_added","post_id":"p_…","topic_id":"t1_…","speaker":"sentry","content":"…","mentions":["bugfix"]}
{"id":"evt_…","ts":"…","kind":"post_superseded","post_id":"p_…","by_post_id":"p_…"}
{"id":"evt_…","ts":"…","kind":"meeting_finished","date":"…"}
```

好处：
- **append-only** + `os.fsync` → 崩溃安全
- **flock advisory lock** → 多进程不会乱序写
- **superseded 字段** → 可以撤回/编辑某条发言（不破坏历史）
- **解析不再依赖 markdown** → agent 输出再脏也不会污染 parser

### 锁机制

- `~/.openclaw/standups/data/<date>/.lock` 是 fcntl 锁文件
- `run_standup.py` 启动前用 `is_locked_exclusive(date)` 检查；server `/api/run` 也检查
- 每次 `append_event()` 短暂持 EX 锁
- 读取时持 SH 锁

### 安全

- HTTP server 默认绑 `127.0.0.1`
- 当 `--host` 不是 loopback 时，**强制要求 bearer token**（自动生成或 `--token` 指定）
- `/api/run` 的 `date` 严格校验 `\d{4}-\d{2}-\d{2}` + `datetime.date.fromisoformat`
- subprocess 用 argv 数组（无 shell 注入）
- 前端：avatar 用 `textContent`，正文先 `escapeHtml` 再做 mention/code 替换

### Squads

Squad 配置文件位于 `~/.openclaw/standups/squads.json`：

```json
{
  "version": 1,
  "squads": {
    "milk-eng": {
      "id": "milk-eng",
      "chair": "milk",
      "members": ["milk", "sentry", "bugfix", "milly", "kb"],
      "emoji": "🥛",
      "name": "milk 工程部",
      "description": ""
    }
  }
}
```

写入通过临时文件加 `os.replace` 完成，避免半写文件。默认 squad 会在首次读取或写入 squad API 时自动创建。

### Web UI

UI 是 Slack 风格三栏：

- 左栏深紫色 `#3F0E40`，展示 `Huddle 🪶` 品牌、`SQUADS` 列表、meeting 数和 `+ New Squad` modal。
- 中栏白底，展示当前 squad 的名称、描述、`MEETINGS` 列表和 `+` 启动按钮；进行中 meeting 有绿色脉动点。
- 右栏是 thread，顶部展示 meeting 标题、chair、前 5 个参会人头像和状态 chip；二级 tabs 为 `opening / T1 / T2 / T3 / closing`；post 使用头像、speaker/time、mention chip、inline code 和 hover 工具栏。

截图描述：左侧为 Slack 紫色 squad rail，中间是按日期倒序的 meeting rail，右侧显示当前 topic thread，底部有暂未开放的 composer。

### HTTP API

```http
GET    /api/squads
GET    /api/squads/<id>
POST   /api/squads
DELETE /api/squads/<id>
POST   /api/squads/<id>/run
GET    /api/standups
GET    /api/standup/<date>
POST   /api/run
```

`POST /api/squads` 要求 `id` 匹配 `\w{1,32}`，`members` 至少一个，`chair` 必须在 members 内。`DELETE /api/squads/milk-eng` 被禁止。`POST /api/squads/<id>/run` 当前复用 `run_standup.py --date <today>`，暂时不传 squad 参数。

### Agent 隔离

每个 agent 用专属 standup session：`standup-<date>-<agent>`，**不会污染主会话**。
明天会议自动用新 session id。

## 命令速查

```bash
# 跑早会（默认今天 / 默认 5 人）
python3 run_standup.py
python3 run_standup.py --date 2026-05-15
python3 run_standup.py --members milk,sentry,bugfix --chair milk
python3 run_standup.py --members milk,sentry --chair milk      # 小规模测试

# Web
python3 server.py                  # 默认 127.0.0.1:7878
python3 server.py --port 7879      # 避开本机已有 7878 服务
python3 server.py --port 8080
python3 server.py --host 0.0.0.0   # 自动生成 bearer token，控制台打印
python3 server.py --host 0.0.0.0 --token mysecret

# 数据
ls ~/.openclaw/standups/data/                       # 所有日期
cat ~/.openclaw/standups/data/2026-05-15/events.jsonl | jq -c
cat ~/.openclaw/standups/standup-2026-05-15.md      # 派生 md（人类视图）
cat ~/.openclaw/standups/squads.json                # squad 配置

# API
curl -sS http://127.0.0.1:7879/api/squads
curl -sS http://127.0.0.1:7879/api/squads/milk-eng
```

## 已知限制 / TODO

- [ ] WebSocket 推送（替代 60s 轮询）
- [ ] meeting_started 写入 squad_id，并让 `/api/squads/<id>` 按真实 squad 过滤
- [ ] 在 web 里点 "reply" 直接给某 agent 发追问（写一条 `post_added` + 指定 parent_post_id）
- [ ] Scott 在 web 里直接发言（speaker=scott 的 post）
- [ ] 跨日期搜索 agent / 关键字
- [ ] 导出 PDF/图片
- [ ] 终端工具：`huddle list / show <date> / undo <post_id>`

## 不做的

- ❌ 多用户登录系统
- ❌ 多机同步 / 服务化部署（这是个本地 viewer）
- ❌ 数据库 — events.jsonl 已经够用，迁 SQLite 是后路
- ❌ 富文本编辑器
