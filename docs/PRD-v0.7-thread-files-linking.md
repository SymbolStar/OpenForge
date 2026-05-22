# PRD: OpenForge v0.7 — Thread ↔ Files Linking

> 版本：v0.7-thread-files-linking
> 日期：2026-05-22
> 负责人：Scott（产品）/ Claude Code（实现）/ Judy（监督）
> 前置：v0.6-files-md 已 ship（commit `45bddf8`）

---

## 1. 背景

v0.6 给 OpenForge 加了左侧 Files 视图，能预览/编辑 `~/.openclaw/openforge/files/*.md`。

但实际用起来，**Thread 和 Files 是两个互不相通的孤岛**：
- Scott 在 thread 里说「看一下 PRD」—— 没人能直接打开它
- Agent 想引用一个文档 —— 只能复制粘贴一坨文本进 thread
- Files 视图里的文档 —— 没法反向追溯「这文档在哪个 thread 被讨论过」

这一版要把这两个面板真正打通，让 Markdown 文件成为 OpenForge 工作流的一等公民。

## 2. 三大核心能力

### 能力 A：多目录根（Workspace Files）
Files 视图必须能看到的**不只是**专用 sandbox 目录。

### 能力 B：Post 里的文件链接渲染
Thread 里出现文件引用 → 渲染成可点击的「在 Files 打开」按钮。

### 能力 C：Agent 工具 — open_file
Agent 在回复时能主动「附带文件给用户看」，前端自动跳 Files 视图打开。

---

## 3. 详细规格

### 3.1 能力 A：多目录根（Multi-Root Files）

#### 3.1.1 目录配置

在 `~/.openclaw/openforge/config.json`（不存在则用 defaults）里定义可见根：

```json
{
  "fileRoots": [
    { "id": "files", "label": "Files", "path": "~/.openclaw/openforge/files", "writable": true },
    { "id": "docs", "label": "Docs", "path": "<openforge_repo>/docs", "writable": true },
    { "id": "readme", "label": "Top-level", "path": "<openforge_repo>", "writable": false, "globs": ["*.md"] }
  ]
}
```

- `id`：路由用的稳定 key（URL-safe）
- `label`：UI 显示
- `path`：绝对路径或 `~` 开头；可用 `<openforge_repo>` 占位符（解析为 server.py 所在目录）
- `writable`：是否允许 PUT/POST（false = 只读）
- `globs`（可选）：限制只暴露匹配的文件（不递归子目录）

如果配置文件不存在，**默认就是当前的 `files` 根**（向后兼容）。

#### 3.1.2 API 变更

| 旧（v0.6） | 新（v0.7） | 备注 |
|---|---|---|
| `GET /api/files` | `GET /api/files?root=<id>` | 不带 root 默认第一个 root；返回里加 `root` 字段 |
| `GET /api/files/<name>` | `GET /api/files/<root>/<name>` | 路径里第一段是 root id |
| `PUT /api/files/<name>` | `PUT /api/files/<root>/<name>` | 只读 root 返 403 |
| `POST /api/files` | `POST /api/files/<root>` | body 仍是 `{name, content?}` |
| — | `GET /api/file-roots` | 新增，返回 `[{id, label, writable, count}]` |

**v0.6 路径需要保留兼容**：旧路由 `/api/files/<name>`（无 root 段）仍然指向第一个 root，但加 `Warning` header 提示 deprecated（下版本删）。

#### 3.1.3 安全
- 所有 root path 在启动时 `resolve()` + 校验存在
- 任何 API 请求里的 `name` 仍然限制为 `[A-Za-z0-9_.-]+\.md`（加上 `.` 支持 `PRD-md-files.md` 这类）
- 解析后用 `Path.resolve()` 二次校验**最终路径必须仍在 root 内**（防 symlink 攻击）
- 只读 root：所有写操作（POST/PUT）→ 403

### 3.2 能力 B：Post 里的文件链接渲染

#### 3.2.1 引用语法

Post / Thread 正文里支持两种语法（marked.js 渲染时做扩展）：

| 写法 | 渲染成 |
|---|---|
| `[[PRD.md]]` | 默认到第一个有这文件的 root，点击跳 `#/files/<root>/PRD.md` |
| `[[docs/PRD.md]]` | 显式指定 root（root id 在前），跳对应 root |
| `[[PRD.md|这份 PRD]]` | 显示「这份 PRD」，hover tooltip 显示文件路径 |

渲染样式：行内 chip
```
📄 PRD.md  ← 浅色背景圆角小标签，hover 高亮
```

#### 3.2.2 实现位置
- 后端：不需要解析；存原文
- 前端：`web/app.js` 里 post 渲染流程中，先用正则替换 `\[\[([^\]|]+)(?:\|([^\]]+))?\]\]` 成可点击 `<a>`，再交给 marked.js
- 跨视图导航：点击 chip → `location.hash = '#/files/<root>/<name>'` → 切到 Files 视图打开文件

#### 3.2.3 自动检测（nice-to-have，本版不做）
未来可以自动识别 post 里的裸文件名「PRD.md」，但本版**只支持显式 `[[...]]`** 避免误判。

### 3.3 能力 C：Agent 工具 — open_file

#### 3.3.1 协议

Agent 回复 post 时，正文里可以附带 OpenClaw-style directive：

```
我已经看完了，重点见 [[docs/PRD.md]]。
MEDIA:openforge-file://docs/PRD.md
```

或更结构化（推荐）的 JSON 行：
```
__openforge_directive__: {"action":"open_file","root":"docs","name":"PRD.md","mode":"side"}
```

前端 post 渲染时识别这些 directive：
- `mode: "side"` → 右侧分屏弹出文件预览（不切走 thread 视图）
- `mode: "switch"` → 直接切到 Files 视图

#### 3.3.2 Agent 端怎么吐
当前 OpenForge 的 agent_runtime 已经在 spawn openclaw 子进程。我们**不改 openclaw 主项目**，做法是：
- 给 agent 的 system prompt 里追加一段说明：「如果用户要看某个文档，回复里加 `[[<root>/<name>.md>]]` 语法」
- 模型自己学着用，不需要改 protocol
- post_router 收到回复时不动手，只是写进 events.jsonl；前端自然渲染

→ 这等于把「让 agent 能 open file」变成**纯 prompt 工程 + 前端渲染**，不动后端 agent 调用链。

### 3.4 Files 视图增强

为了承接多 root：
- Files Rail 顶部加 **root selector**（dropdown 或 tab）
- 列表分组显示当前 root 的文件
- 只读 root 下，「+ 新建」和「保存」按钮 disable + tooltip「此目录只读」
- URL 路由：`#/files/<root>/<name>` （root 段必填）

---

## 4. 用户故事验收

- **US1**：Scott 在 thread 里发「看一下 [[docs/PRD-md-files.md]]」→ 这个 chip 可点击 → 点了切到 Files 视图、Docs root、PRD-md-files.md 打开
- **US2**：Scott 在 Files 顶部切到 Docs root → 看到 `PRD-md-files.md` / `PRD.md` 等
- **US3**：Scott 在 Top-level root 下尝试编辑 README.md → 编辑按钮 disabled，提示「只读」
- **US4**：Agent 回复「PRD 在 [[docs/PRD-md-files.md]] 里」→ 用户看到带 📄 图标的 chip，点了直接看到 PRD
- **US5**：旧 URL `#/files/foo.md` 仍能打开（兼容默认 root）

## 5. 测试要求

### 5.1 后端单元测试
- `tests/test_file_roots.py`：
  - 配置加载（默认 + 自定义 + path 不存在的 root 跳过/警告）
  - `GET /api/file-roots` 列出所有 root
  - 多 root 下 `GET /api/files?root=docs` 返回正确
  - 只读 root 的 PUT/POST → 403
  - 路径穿越（root=`../`, name=`../../etc/passwd`）→ 400
  - v0.6 兼容：`/api/files/<name>` 仍然工作 + 带 deprecation header

### 5.2 前端
- `tests/test_post_render.py`（如果决定加 server-side 渲染单元测）—— 否则跳过
- 手动 Playwright happy path：发 post 含 `[[docs/PRD.md]]` → chip 可点击 → 跳转正确

### 5.3 Coverage
- 整体仍 ≥80%（v0.6 codex 拉到了这个数）
- 新代码 100% 覆盖

### 5.4 Smoke
ci.yml 的 smoke job 加一条：
- `GET /api/file-roots` 返回非空
- 写一个 root config 文件，重启 server，验证多 root 生效
- 只读 root 的 POST → 403

## 6. 验收 Checklist

- [ ] `~/.openclaw/openforge/config.json` 支持 `fileRoots`
- [ ] `GET /api/file-roots` 新路由 OK
- [ ] 所有 file API 支持 `<root>/<name>` 段
- [ ] v0.6 旧路由兼容 + deprecation header
- [ ] Files Rail 有 root selector
- [ ] 只读 root 的 UI 禁用写操作
- [ ] Post 里 `[[root/file.md]]` 渲染成可点 chip
- [ ] 点 chip → 切 Files 视图 + 打开对应文件
- [ ] Agent system prompt 已附加链接语法说明
- [ ] 路径穿越攻击拦截
- [ ] pytest 全过，coverage ≥80%
- [ ] CI 全绿
- [ ] 多条 conventional commit 推到 main

## 7. Out of Scope

- 子目录递归（root 仍是平铺）
- 文件删除 / 重命名
- 非 md 文件
- 自动检测裸文件名（无 `[[]]` 包裹的）
- 文件 → thread 反向链接（「这文件在哪些 thread 被提过」）
- 右侧分屏 / split view（agent directive 的 `mode: side` 本版**降级为 switch**，分屏留下版）
- 协作编辑、版本历史

## 8. 实现顺序（推荐）

1. **20 分钟**：后端多 root + 配置加载 + `/api/file-roots` + 兼容旧路由 + 测试
2. **15 分钟**：前端 root selector + 只读处理 + URL 改 `#/files/<root>/<name>`
3. **15 分钟**：post 渲染 `[[...]]` chip + 点击跳转
4. **10 分钟**：Agent system prompt 附加说明 + smoke + 文档
5. **buffer**：CI 红了修，文档收尾

## 9. 风险与回退

- **风险**：v0.6 已发的 `45bddf8` 改动较大；要把 API 路径加 root 段是 breaking change。**缓解**：保留旧路由 + deprecation header，下版本（v0.8）再删
- **风险**：marked.js 的扩展机制有学习成本。**缓解**：直接用正则在 marked 之前预处理字符串，简单粗暴
- **回退**：如果 Agent 工具（能力 C）不顺，**只交付 A + B**；C 留下版（agent 端能力本来就是 prompt 工程，可以延后）
