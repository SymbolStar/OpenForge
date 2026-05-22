# PRD: OpenForge v0.9 — Agent Context Bundle

> 版本：v0.9-agent-context-bundle
> 日期：2026-05-22
> 负责人：Scott（产品）/ Claude Code（实现）/ Judy（监督）
> 战略定位：**坚定「数字员工」路线** —— 让 OpenForge spawn 的 agent 子 session 不再是「失忆的临时工」，而是「带着完整上下文上场的同事」

---

## 1. 背景

### 1.1 实测痛点
Scott @sherry 在 OpenForge thread 里问「日报做完了吗」→ 子 session 一片空白 → 回答时不知道**主 session 早就做了**，浪费 turn 去查或重复劳动。

### 1.2 业界扫描（详见 docs/memory-architecture-comparison.md）
- **MemGPT/Letta**：core + recall + archival 三层内存，agent 自己 self-edit。**单 session 范式**
- **OpenAI Assistants**：thread 持久化但跨 thread 完全隔离
- **Anthropic Subagent**：一次性 fork，单向，2025-10 加了 file-based memory tool
- **AutoGen/CrewAI**：memory 当插件，多用户场景实测打架
- **Multica**：每 task 全新 sandbox，靠 skill 库（pgvector）做组织资产复用

**结论**：**没有一家在做「同 agent 多并行 session 自动融合」**。OpenForge 在这条赛道是空白领跑者。

### 1.3 战略定位

| 路线 | Multica | OpenForge |
|---|---|---|
| 哲学 | Agent as worker | **Agent as colleague** |
| 复利方向 | 横向：skill 库 | **纵向：每个 agent 越用越懂** |
| 抽象 | Task / Issue | **Thread / Conversation + Persistent Identity** |
| 卖点 | "Assign tasks, harvest skills" | **"They remember, they grow, they have status"** |

## 2. 目标

让 OpenForge spawn 的 sherry 子 session 一启动就**自动具备**：
1. Sherry **自己当前在做什么**（STATUS.md）
2. Sherry **主 session 最近的活动摘要**（去重，避免重复劳动）
3. Sherry **沉淀过的相关记忆**（memory_search）

实现路径借鉴：
- Letta 的「core memory 永远注入」
- Anthropic 2025-10 的「file-based memory tool」
- Letta 2026 benchmark 洞察「filesystem is enough」
- 避开 CrewAI 「memory 当插件」的 retrofit 坑 → **day 1 设计成核心**

## 3. 范围

### ✅ In scope
1. **STATUS.md 标准化**：约定文件位置、schema、写入接口
2. **Context Bundle 自动注入**：OpenForge spawn 子 session 时拼三源 context 注入 task message
3. **`update_status` 工具**：给 agent 主动维护 STATUS.md 的 CLI / HTTP 接口
4. **主 session 识别**：每个 agent 配 `mainSessionKey`，可被 sessions_history 拉取
5. **可配置策略**：每个 agent 自定义注入哪些源、截断多长
6. **Bundle 缓存**：短时间内复用 bundle 避免反复查

### ❌ Out of scope
- 双向状态同步（child → main 回流）—— v1.0
- 跨 agent 状态共享（sherry 知道 milk 在干啥）—— v1.0
- Memory hierarchical paging（Letta 路线）—— 不需要，文件够用
- 自动从主 session 提取 STATUS（让 agent 自己写就好）
- Cross-thread / cross-squad 状态联动 —— v1.0
- Multi-tenant 隔离 —— 当前单用户场景不需要

---

## 4. 数据模型

### 4.1 STATUS.md 标准 schema

文件位置：`~/.openclaw/workspace-<agent>/STATUS.md`

格式（半结构化 markdown，agent 易读易写）：

```markdown
# <Agent Name> STATUS

> 最后更新：2026-05-22 16:35 by sherry-main

## 当前焦点
正在做：CN 日报 v3 草稿
状态：✅ 已入箱（feishu/scott DM）
下一步：等 Scott 回复确认

## 进行中任务
- [ ] Foundry agent 性能 review（卡在 quota，等 Microsoft 回复）
- [x] CN 日报 2026-05-22 草稿 → ✅ 16:35 已入箱
- [ ] Sherry workspace 日记整理（计划本周末）

## 已知 blocker
- Foundry quota：ticket #INC123，预计 2026-05-23 回复

## 给我的协作者
- Scott：CN 日报草稿在 feishu DM，标题「【日报】2026-05-22」
- Milk：本周三例会 ETA 是周日，需要她的 KPI 数据
```

**约定**：
- 顶部 `> 最后更新：...` 行由系统自动维护
- 「当前焦点」必填，一段话内
- 其他章节灵活，每 agent 可以自己加

### 4.2 Agent 主 session 映射

`~/.openclaw/openforge/config.json` 扩展：

```jsonc
{
  "fileRoots": [ ... ],  // v0.7
  "agents": {
    "sherry": {
      "mainSessionKey": "agent:sherry:main",
      "contextBundle": {
        "enabled": true,
        "include": ["status", "main_session", "memory"],
        "main_session_turns": 20,
        "memory_top_k": 5,
        "status_max_bytes": 4096,
        "main_session_max_bytes": 8192,
        "cache_ttl_seconds": 60
      }
    },
    "milk": {
      "mainSessionKey": "agent:milk:main",
      "contextBundle": { "enabled": true }  // 用默认值
    }
  }
}
```

默认值（agent 没配置时）：
- `include`: `["status", "main_session", "memory"]`
- `main_session_turns`: 20
- `memory_top_k`: 5
- 各 size cap：合理默认
- `cache_ttl_seconds`: 60

### 4.3 Bundle 缓存

`~/.openclaw/openforge/context_bundles/<agent>.json`（短期 cache）：

```jsonc
{
  "agent": "sherry",
  "generated_at": 1779435000,
  "expires_at": 1779435060,
  "sources": {
    "status": "...content...",
    "main_session": "...summary...",
    "memory": [{"path": "...", "snippet": "..."}, ...]
  },
  "size_bytes": 6234
}
```

## 5. API 设计

### 5.1 Context Bundle 生成（内部）

`forge_context.py` 新模块：

```python
def build_context_bundle(
    agent_id: str,
    query_hint: str | None = None,  # 用于 memory_search 的提示
    force_refresh: bool = False,
) -> ContextBundle:
    """
    拼三源 context。从 config 读策略，按 cache TTL 复用。
    返回结构化对象 + render 成 markdown 的方法。
    """
```

实现：
- **STATUS.md** 源：直接读 `~/.openclaw/workspace-<agent>/STATUS.md`，截断到 max_bytes
- **主 session** 源：调 OpenClaw 的 `sessions_history` API（已有），取 last N turns 转 markdown 摘要
- **Memory** 源：调 OpenClaw 的 `memory_search` API，query 用 thread 主题（first post 或 thread title）
- 全部塞进 bundle 对象 → 序列化为 markdown 块

### 5.2 Agent 更新 STATUS（HTTP）

```
POST /api/agents/<id>/status
Body: { "content": "...full markdown..." }
→ 200 { "agent": "sherry", "size": 1234, "updated_at": ... }

PATCH /api/agents/<id>/status
Body: { "section": "当前焦点", "content": "..." }
→ 200 (局部更新某个 section)

GET /api/agents/<id>/status
→ 200 { "agent": "sherry", "content": "...", "updated_at": ... }
```

权限：暂时无认证（与 v0.8 refs 一致，agent 是 trusted side）

### 5.3 Agent CLI helper（可选，下版）

```bash
forge status set "正在做 CN 日报 v3 草稿"
forge status append-task "review Foundry quota"
forge status done "CN 日报草稿"
```

本版**不做** CLI，agent 直接 curl 即可（参考 v0.8 file refs 的 prompt 工程路径）。

### 5.4 Bundle Preview（调试用）

```
GET /api/agents/<id>/context-bundle?refresh=1
→ 200 { ...full bundle... }
```

供前端 UI 显示「sherry 当前 context bundle 长啥样」用。

---

## 6. 集成点：OpenForge spawn agent 流程

修改 `post_router.py`：

```python
def _route_to_agent(thread_id, post, agent_id):
    # ... existing snapshot/restore logic ...

    # ✨ NEW: build context bundle
    bundle = forge_context.build_context_bundle(
        agent_id=agent_id,
        query_hint=_extract_thread_topic(thread_id),
    )

    # Wrap user message with bundle preamble
    enriched_message = f"""\
## 你的最新上下文（OpenForge 已预查，请基于此回复）

{bundle.render()}

---

## 用户消息（thread {thread_id}）
{post['content']}
"""

    # ... existing call_agent logic, 把 enriched_message 传进去 ...
```

注意：
- **bundle 在 spawn 前由 OpenForge 代理收集**，agent 不用自己查
- 失败优雅降级：bundle 任何源出问题，跳过那一源继续
- 命中 cache 直接复用，不重复 sessions_history

---

## 7. 前端 UI

### 7.1 Agent Status 卡片
在 OpenForge 左侧 Icon Rail 加新入口 **🧑 Agents**：
- 列出所有配置的 agent
- 每个 agent 卡片显示：头像 + 名字 + STATUS.md「当前焦点」一段
- 点开 → 右侧 pane 显示完整 STATUS.md（marked.js 渲染）+ 上次更新时间

### 7.2 Context Bundle Preview
Agent 详情页加个 collapsible 「Last Context Bundle」section：
- 显示最近一次注入的 bundle 内容
- 帮助调试「为什么 sherry 知道 / 不知道某事」

### 7.3 Thread 里的可视化提示
当一个 post 触发了 agent 回复 + bundle 注入：
- 该回复下方加小标签：「🧠 用到了 STATUS（3min 前）+ 主 session（20 turns）+ 5 条 memory」
- 点击展开能看具体注入了什么

---

## 8. Agent system prompt 升级

`agent_runtime.py` 拼装 system prompt 时追加：

```markdown
## 你的工作记忆

你的 STATUS.md 是「你大脑里的当前焦点」，永远在 OpenForge 给你的 context 里。
**回复前先看 bundle 里的「STATUS」段**，不要去重复 STATUS 里已经记录的事。

完成阶段性工作时，主动更新你的 STATUS：

```bash
curl -sX POST http://127.0.0.1:7878/api/agents/<your-id>/status \
  -H "Content-Type: application/json" \
  -d @- <<EOF
{
  "content": "...你的新 STATUS.md 完整内容..."
}
EOF
```

或者只更新一个 section：
```bash
curl -sX PATCH ... -d '{"section":"当前焦点","content":"正在做 X"}'
```

约定：
- 「当前焦点」必填，一句话说清楚此刻在做什么
- 进度变更（开始/卡住/完成）就更新
- 不要等到「全部做完」再写
```

---

## 9. 测试要求

### 9.1 单元
- `tests/test_context_bundle.py`：
  - STATUS 源：文件存在/不存在/超大截断
  - main_session 源：sessions_history 成功/失败/timeout
  - memory 源：memory_search 成功/无结果/失败
  - Cache hit / miss / TTL 过期
  - 全部源失败 → bundle 仍能返回空 markdown
- `tests/test_agent_status_api.py`：
  - POST 全量更新
  - PATCH 局部更新（不存在的 section 报错）
  - GET 读取
  - 路径穿越攻击拦截

### 9.2 集成
- spawn agent 流程：mock sessions_history + memory_search，验证 enriched_message 内容正确
- 实测：手动配 sherry，spawn 一次，看 prompt 里是否有 bundle

### 9.3 Coverage
- 整体 ≥80%（v0.7 gate 不破）
- 新代码 ≥85%

### 9.4 Smoke
ci.yml smoke 加：
- POST /api/agents/test-agent/status
- GET /api/agents/test-agent/context-bundle
- 校验 bundle 结构

---

## 10. 验收 Checklist

- [ ] `forge_context.py` 新模块
- [ ] `~/.openclaw/openforge/config.json` `agents.<id>.contextBundle` 配置
- [ ] `~/.openclaw/openforge/context_bundles/` cache 目录
- [ ] `build_context_bundle()` 拼三源
- [ ] Cache TTL 生效
- [ ] 各源失败优雅降级
- [ ] `POST/PATCH/GET /api/agents/<id>/status` 三个新路由
- [ ] `GET /api/agents/<id>/context-bundle` debug 路由
- [ ] `post_router.py` spawn 前注入 bundle
- [ ] `agent_runtime.py` system prompt 教 agent 主动写 STATUS
- [ ] 前端 Icon Rail 加 🧑 Agents 入口
- [ ] Agent 详情页显示 STATUS.md
- [ ] Thread post 旁显示 bundle 注入提示
- [ ] pytest 全过，coverage ≥80%
- [ ] CI 全绿
- [ ] 实测：sherry STATUS 写「日报已入箱」→ Scott @sherry 「日报做完了吗」→ 子 session 一次性答出「16:35 已入箱」（不去查不重做）

---

## 11. 实现顺序

| 阶段 | 时间 | 内容 |
|---|---|---|
| 1 | 25 min | `forge_context.py` 模块 + 三源拼装 + cache + 单元测试 |
| 2 | 15 min | `POST/PATCH/GET /api/agents/<id>/status` + 测试 |
| 3 | 10 min | `post_router.py` 集成 bundle 注入 |
| 4 | 5 min | `agent_runtime.py` system prompt 追加 |
| 5 | 15 min | 前端 Agents 入口 + Status 卡片 + bundle preview |
| 6 | 10 min | smoke + 文档 + 推 main + 等 CI |
| **总计** | **80 min** | |

---

## 12. 风险与回退

| 风险 | 缓解 |
|---|---|
| Context 注入过长，token 爆 | 各源严格 size cap；超长走 summary（截断 + 提示） |
| sessions_history API 慢或失败 | Cache + timeout（3s）+ 失败跳过 |
| Agent 不愿意主动写 STATUS | 提示词强调 + 加 UI 提醒 + 后期可加 cron 提醒 |
| 主 session key 命名不一致 | config 显式声明 mainSessionKey，无默认魔法 |
| STATUS.md 多 agent 并发写冲突 | 单文件 atomic write（tmp + rename） |
| 缓存数据陈旧导致用户困惑 | UI 显示 bundle generated_at；force_refresh 选项；TTL 默认 60s 短一点 |

**回退**：如果前端 UI 来不及（阶段 5）→ 先交付后端 + 集成 + 提示词（阶段 1-4），UI 留 v0.9.1

---

## 13. 长期愿景（v1.0+）

本 v0.9 只做**单向注入**（main → openforge spawn）。未来：

| 版本 | 能力 |
|---|---|
| **v1.0** | 双向：openforge spawn → 写回 main 一条 event「Scott 在 thread X 问了 Y」 |
| **v1.1** | 跨 agent：sherry 能看 milk 当前 STATUS（公开部分） |
| **v1.2** | Auto-extract：每隔 N 分钟从主 session 自动提取 STATUS（不靠 agent 自律） |
| **v1.3** | Status timeline：STATUS.md 改成 event stream，可看历史 |
| **v2.0** | "Agent Console"：每个 agent 自己的 dashboard，像 Notion 一样能编辑自己的状态/任务/笔记 |

---

## 14. 一句话对外定位

> **"OpenForge agents remember what they're doing — even across rooms."**

或中文：
> **「OpenForge 的数字员工，不只是会说话，还记得自己在干啥。」**

这是 Multica（任务编排）/ MemGPT（单 agent 内存）/ Anthropic subagent（一次性）都没占的位置。
