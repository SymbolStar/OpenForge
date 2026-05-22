# Case Study：v0.9 Context Bundle 实战 trace

> **场景**：用户在 OpenForge thread 里 @ 一个 agent，给它下达一条新规则（约束未来的某类输出格式）
>
> **耗时**：从用户发 post 到 agent 给出最终回复 + 实际文件落地 = **约 37 秒**
> **成本**：output 50 tokens，cacheRead 41,759，cost $0（OpenClaw prompt cache 命中）
> **效果**：agent 没反问、没重复劳动、直接修改了相关的 review checklist 文件

这是 v0.9（context bundle）+ v0.9.1（memory 改 on-demand）落地后第一个真实端到端 case，存档供架构迭代参考。**所有用户/业务细节已脱敏，本文只保留架构层面可公开内容。**

---

## 1. 全链路时序（毫秒级）

| 时间 | 谁 | 动作 |
|---|---|---|
| T+0ms | 用户 | POST `/api/threads/.../posts`，正文 `@<agent> ...` |
| T+8ms | post_router | 检测 @ → 立刻 enqueue + 写占位 post (`__router__ ⏳ 正在思考中…`，parent = 用户那条) |
| T+8ms | post_router | snapshot agent 主 session（防污染） |
| T+10ms | forge_context | `build_context_bundle(agent)` 拼两源：STATUS + 主 session 最近 N turns |
| T+50ms | post_router | spawn 子 session：`openclaw call agent <id> --session forge-<tid>-<agent> --local --json` |
| T+10s | agent sub | 子 session 起好，模型开始推理 |
| T+18s | agent sub | 第一段 text 回复（先答用户） |
| T+20s | agent sub | tool: `grep` 定位相关文件 |
| T+22s | agent sub | tool: `read` 读上下文 |
| T+30s | agent sub | tool: `edit` 修改文件 |
| T+34s | agent sub | 第二段 text 回复（确认落地） |
| T+37.4s | post_router | append `post_added` (speaker=agent) 到 thread events.jsonl |
| T+37.4s | post_router | 占位 post 标 `post_superseded` |
| T+46s | 用户 | 反馈 reaction (👍) |

**核心数字**：spawn + 推理占约 24 秒，4 次工具调用 + 落地占约 13 秒，**端到端 ~37 秒**。

---

## 2. Bundle 注入结构（实测）

OpenForge spawn agent 子 session 时塞进的 user message 开头：

```markdown
## 你的最新上下文（OpenForge 已预查，请基于此回复）

_OpenForge context bundle generated <ts>_

### 📋 STATUS（agent 自己维护，更新于 <ts>）

# STATUS.md - <Agent> 状态管理
最后更新：<ts>

## 任务清单
### TASK-NNN <略>
- state: doing
- next_check_at: <ts>
- ...
[~4KB 截断到 status_max_bytes]

### 🧵 主 session 最近 N 条 turn (agent:<id>:main)

**🧑 you**: <最近的 assistant 输出摘要>
**👤 user**: <最近的 user 输入摘要>
...
[~8KB 截断到 main_session_max_bytes]

━━━ Thread ━━━
#### <user> · <ts>    <最新 post>
━━━ 结束 ━━━

[文件引用语法 v0.8]      ← refs API 教学
[STATUS 自维护说明 v0.9]  ← curl POST /api/agents/<id>/status 教学
[需要历史细节？v0.9.1]    ← memory_search(query="...") 教学
```

**关键点**：bundle 不预查 memory，但 prompt 明确告诉 agent「需要历史细节时调 `memory_search`」。这次 agent 判断**不需要**查历史 → 没调 → 省 token。

---

## 3. Agent 实际 action 链（trajectory 还原）

```
msg #N    user   <bundle preamble + thread + tooling hints>
msg #N+1  assistant TEXT     "收到，规则已记录..."          ← 先答用户
msg #N+2  toolUse  grep      pattern=<新规则关键词>
msg #N+3  toolResult         <命中位置>
msg #N+4  toolUse  read      <目标 checklist 文件>
msg #N+5  toolResult         <文件当前内容>
msg #N+6  toolUse  edit      replace <旧规则> → <新规则>
msg #N+7  toolResult         Successfully replaced N block(s)
msg #N+8  assistant TEXT     "规则已落地..."
```

**没有** `memory_search` 调用 —— 符合 v0.9.1 设计哲学（这次不需要历史）。

---

## 4. v0.9 / v0.9.1 设计目标 vs 实测兑现

| 设计目标 | 兑现情况 |
|---|---|
| 子 session 知道主 session 在干啥 | ✅ Bundle 注入了主 session 最近 N turns |
| 子 session 知道 agent 自己当前任务 | ✅ STATUS.md 全文截断后注入 |
| 不反问、不重复劳动 | ✅ 直接处理新规则，没复述、没问用户 |
| 不预查 memory（on-demand 哲学） | ✅ 本次不需要历史 → agent 没调 → 省 token |
| Prompt cache 命中省钱 | ✅ cacheRead 41,759 / cacheWrite 640 / cost $0 |
| 占位 → 真回复无缝替换 | ✅ T+8ms 占位 → T+37.4s superseded |

---

## 5. 下版需要改进的（已识别）

1. **STATUS.md 截断策略**：当前从头截到 max_bytes，会丢失「最新焦点」。下版改成「最新焦点 + 最近 N 个 done 摘要」的智能截断
2. **主 session 噪音**：注入的 N turns 里含 heartbeat / 系统事件，占 bundle 空间。本期保留以观察行为，下版考虑加 filter 开关
3. **占位 post 缺工具进度**：用户在 UI 只看到 ⏳，看不到 agent 正在调什么工具。下版可让 router 透传 tool_use name 到占位 post
4. **配置化**：当前 bundle 注入策略写死，应该让每 agent 独立配 `include` / `*_max_bytes` / `cache_ttl_seconds`

---

## 6. v0.9 之前 vs 之后（架构层面对比）

| 维度 | v0.9 之前 | v0.9 之后 |
|---|---|---|
| 子 session 起点是否知道主 session | ❌ 完全空白 | ✅ N turns 注入 |
| 子 session 是否知道 agent 当前任务 | ❌ 空白 | ✅ STATUS 注入 |
| 行为：是否反问 / 翻历史 | 通常会 | ✅ 直接动手 |
| 端到端响应时间 | 通常 60-90s | **~37s** |
| Output tokens | 多（含解释 + 反问） | output 50 / cacheRead 41K |

---

## 7. 一句话总结

> **从「失忆临时工」到「带着完整上下文上场的同事」**。
> 同一个 agent，没改提示词、没改业务逻辑，**仅靠 bundle 注入**，端到端就从「先翻一遍 history 才能回答」变成「37 秒内答 + 改 + 落」。

这是 OpenForge「**数字员工**」路线的第一个可演示节点。
