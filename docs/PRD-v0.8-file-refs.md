# PRD: OpenForge v0.8 — File References (Distributed Files)

> 版本：v0.8-file-refs
> 日期：2026-05-22
> 负责人：Scott（产品）/ Claude Code（实现）/ Judy（监督）
> 前置：v0.7-thread-files-linking 已 ship

---

## 1. 背景与动机

v0.7 的 `fileRoots` 配置假设「文件都集中在 OpenForge 配置的目录里」，但实际情况是：
- 每个 agent（milk / sentry / judy / …）在**自己的 workspace** 生成文件
- 让用户去手动配 fileRoots 把每个 agent workspace 列进去 — 不可扩展
- 让 agent 都把文件复制到 `~/.openclaw/openforge/files/` — 违反 agent 自治原则

**v0.7 实测踩坑**：milk 在 thread 里发了 `[[c3-stability-2026-05-22.md]]`，文件实际在 `~/.openclaw/workspace-milk/`，但没配 fileRoot → 点击 chip 404。

### 核心理念转变

| 角度 | v0.7 | v0.8 |
|---|---|---|
| Files 是什么 | 物理目录（fileRoots） | **引用注册表**（refs） |
| 文件归属 | OpenForge 沙箱 | 各 agent workspace 自治 |
| 谁注册 | 用户改 config | Agent 自己调 API |
| 解决 404 | 改配置 | 自动 |

## 2. 范围

### ✅ In scope
1. 新模块 `forge_refs.py`：引用注册表（append-only jsonl）
2. 新 API `/api/refs/*`（POST 注册 / GET 列表 / GET 内容 / DELETE 注销）
3. 引用语法解析：`[[ref:<id>]]` / `[[<agent>/<label>]]` / `[[<label>]]`
4. 前端 Files 视图新增 **References tab**，按 agent 分组
5. 前端 chip 渲染优先查 refs，未命中再查 v0.7 fileRoots（兼容）
6. Agent system prompt 更新，教 agent 注册 ref + 用 ref 语法
7. 安全：读取时校验文件存在/大小/类型；可选每 agent 路径白名单
8. v0.7 `fileRoots` 完全保留，向后兼容

### ❌ Out of scope
- 文件版本快照（v0.9）
- ACL（哪个用户能看哪个 ref）
- 跨 thread / squad 的 ref 使用统计
- 文件 thumbnail / preview（图片直接渲染即可）
- 编辑非 markdown 文件（这版仍只支持 md 编辑）
- 删除 abs_path 的物理文件（DELETE 只注销 ref，不动文件）
- ref 过期机制 / 垃圾回收

## 3. 数据模型

### 3.1 Ref 记录

```jsonc
{
  "id": "ref_a3f7b2",            // 短 sha 风格 ID，前缀 ref_
  "label": "c3-stability-2026-05-22.md",  // 显示名（建议 = basename）
  "abs_path": "/Users/symbolstar/.openclaw/workspace-milk/c3-stability-2026-05-22.md",
  "source_agent": "milk",        // 谁注册的（必填，前端按这个分组）
  "thread_id": "th_abc123",      // 可选，关联 thread
  "squad_id": "milk-eng",        // 可选，关联 squad
  "registered_at": 1779432000,
  "content_type": "text/markdown", // 可选，前端决定渲染方式
  "writable": false,             // 是否允许 PUT 写回（默认 false 安全）
  "size_hint": 4823              // 注册时的大小，仅供前端展示
}
```

### 3.2 存储

```
~/.openclaw/openforge/refs.jsonl
```
- Append-only JSONL（每行一条记录）
- 类型字段隐式：`{"op": "register", ...}` / `{"op": "unregister", "id": "..."}`
- 启动时 in-memory 回放，按 id 维护活跃 refs（unregister 标记 dead）

### 3.3 ID 生成

`ref_` + 6 位 [a-z0-9] → `ref_a3f7b2`
冲突极少；冲突时重试。

## 4. API 设计

### 4.1 注册

```
POST /api/refs
Body: {
  "label": "c3-stability-2026-05-22.md",
  "abs_path": "/abs/path/to/file.md",
  "source_agent": "milk",
  "thread_id": "th_..." (optional),
  "squad_id": "..." (optional),
  "writable": false (optional, default false)
}
→ 201 { "id": "ref_a3f7b2", ...full ref... }
```

校验：
- abs_path 必须绝对路径
- 文件必须存在 + 可读
- 文件大小 ≤ **10 MB**（超限 413）
- source_agent 必填且非空
- label 非空
- 重复注册（同 abs_path + source_agent）→ 返回已有 ref id（idempotent）

### 4.2 列表

```
GET /api/refs?agent=milk&thread=th_xxx&squad=milk-eng
→ 200 { "refs": [ ...list of active refs... ] }
```

支持 query filter（任意组合 OR null）。默认按 `registered_at` 倒序。

### 4.3 元数据

```
GET /api/refs/<id>
→ 200 { ...ref... }
→ 404 if not found / unregistered
```

### 4.4 读取内容

```
GET /api/refs/<id>/content
→ 200 application/octet-stream + headers:
  Content-Type: <detected or registered>
  X-Ref-Label: <label>
  X-Ref-Source-Agent: <agent>
→ 404 if ref not found OR file gone OR file too big
→ 403 if MIME type blocked
```

MIME 白名单：
- `text/*`（md / txt / json / yaml / log）
- `image/*`（png / jpg / gif / webp）
- `application/json`
- 其他全部 403

### 4.5 写回

```
PUT /api/refs/<id>/content
Content-Type: text/plain
Body: <new content>
→ 200 { "id": "...", "size": <new>, "mtime": <ts> }
→ 403 if ref.writable=false
→ 413 if > 10 MB
```

### 4.6 注销

```
DELETE /api/refs/<id>
→ 204
```
只 append 一条 `{"op": "unregister", "id": "..."}` 到 jsonl，不动物理文件。

## 5. 安全

### 5.1 读取防御
- abs_path 用 `Path(...).resolve()` 解析，校验文件存在 + 是 file（不是 socket / device）
- 大小上限 10 MB，超过 413
- MIME 白名单（§4.4）
- 不解析 symlink（`is_symlink()` → 403）

### 5.2 注册防御
- abs_path 必须是绝对路径
- 可选：每个 agent 注册的 abs_path 必须在 `~/.openclaw/workspace-<source_agent>/` 下（开关 `restrict_to_agent_workspace=true`，默认 true）
- 路径穿越攻击：注册时 `Path.resolve()` 后校验前缀

### 5.3 防遍历
- 无法通过 `[[../etc/passwd]]` 拿任何东西，因为前端 chip 语法只解析 ref id / agent/label，最后都走 ref 记录里的 abs_path
- 不存在「通过 chip 提交任意 abs_path」的路径

## 6. 前端

### 6.1 Chip 语法（marked 前预处理）

| 写法 | 解析 |
|---|---|
| `[[ref:a3f7b2]]` | id 精确匹配 |
| `[[milk/c3-stability-2026-05-22.md]]` | `source_agent + label` |
| `[[c3-stability-2026-05-22.md]]` | 全局 label 唯一匹配；多个 → chip 仍渲染但 hover 提示「多义」，点击进 References tab 过滤 |
| `[[docs/PRD.md]]`（v0.7 风格） | **先查 refs（agent=docs, label=PRD.md）**；未命中 → 回退到 v0.7 fileRoots 解析 |

### 6.2 Files 视图改造

- Icon Rail 的「Files」入口不变
- 视图顶部 tab：
  - **References**（默认）：所有 refs，按 source_agent 折叠分组
  - **Workspace Files**（v0.7 fileRoots）：保留原有 root selector
- References 列表项：label + 文件大小 + 注册时间 + source agent 头像/emoji
- 点击 → 右侧 pane 渲染内容（md → marked / 图片 → `<img>` / json → 高亮 / 其他 → `<pre>`）
- 编辑按钮：仅当 ref.writable=true 显示
- 顶部搜索框：实时过滤 label

### 6.3 跨视图跳转

点击 chip → `location.hash = '#/files/refs/<ref_id>'` → Files 视图 References tab 自动选中并打开

URL scheme：
- `#/files/refs/<id>` — 单个 ref 详情
- `#/files/refs` — References tab 列表
- `#/files/root/<root>/<name>` — v0.7 workspace files（不变）
- `#/files/<name>` — v0.7 legacy（兼容）

## 7. Agent 集成

### 7.1 System prompt 追加

在 `agent_runtime.py` 系统提示里增加段落：

```
## 给用户发送文档

当你生成了一个文件（报告 / 总结 / 代码 / 截图）想让用户能在 OpenForge 里点开看：

1. 先把文件写到你自己的 workspace（你已经在做）
2. **注册到 OpenForge refs**：
   curl -sX POST http://127.0.0.1:7878/api/refs \
     -H "Content-Type: application/json" \
     -d '{
       "label": "<filename>",
       "abs_path": "<absolute path you wrote to>",
       "source_agent": "<your agent id>"
     }'
3. 回复里用引用语法：
   - 推荐：[[<your-agent-id>/<filename>]] — 稳定且人类友好
   - 备用：[[<filename>]] — 简短，仅当 label 全局唯一时
   - 高级：[[ref:<id>]] — 注册返回的 id

例：milk 生成了 c3-stability-2026-05-22.md → 注册 → 在回复里写
「扫完了 → [[milk/c3-stability-2026-05-22.md]]」

注意：abs_path 必须绝对路径；同一文件重复注册返回相同 ref id（幂等）。
```

### 7.2 不强制改 openclaw 协议
保持 v0.7 思路：纯 prompt 工程 + 简单 REST API，模型自然学会。

### 7.3 可选辅助
未来可以提供一个 helper script `bin/forge ref add <path> <agent>` 让 agent 一条命令搞定，但本版不做。

## 8. 测试

### 8.1 单元
- `tests/test_refs.py`：
  - register 成功 / 失败（abs_path 不存在、不可读、太大、不是绝对路径、symlink）
  - 重复注册幂等（同 abs_path + agent → 同 id）
  - list 全部 / 按 agent 过滤 / 按 thread 过滤 / 按 squad 过滤
  - get content：md / txt / json / 图片 ok；blocked MIME 403
  - PUT content：writable=true 成功，writable=false 403，超大 413
  - DELETE 注销 + 后续 GET 404
  - unregister 后重新注册（新 id）
  - jsonl 持久化 + 重启回放

### 8.2 集成 / smoke
ci.yml smoke 加：
- POST 注册一个 tmp md 文件
- GET 列表确认
- GET content 拿回内容
- DELETE
- GET 404

### 8.3 Coverage
新代码 ≥ 80%，整体 ≥ 80%（gate 不破）

## 9. 验收 Checklist

- [ ] `forge_refs.py` 新模块
- [ ] `~/.openclaw/openforge/refs.jsonl` append-only 存储
- [ ] `POST /api/refs` 注册（幂等）
- [ ] `GET /api/refs` 列表 + 过滤
- [ ] `GET /api/refs/<id>` 元数据
- [ ] `GET /api/refs/<id>/content` 读取（MIME 白名单 + 大小限制）
- [ ] `PUT /api/refs/<id>/content` 写回（受 writable 控制）
- [ ] `DELETE /api/refs/<id>` 注销
- [ ] 前端 References tab + 按 agent 分组
- [ ] 前端 chip `[[ref:id]]` / `[[agent/label]]` / `[[label]]` 都能解析
- [ ] 点 chip 跳转 `#/files/refs/<id>`
- [ ] v0.7 fileRoots 路径不破，chip 自动 fallback
- [ ] agent_runtime.py system prompt 已追加注册说明
- [ ] 路径穿越 / symlink / blocked MIME / 超大全部拦
- [ ] pytest 全过，coverage ≥80%
- [ ] CI 全绿
- [ ] 多条 conventional commits 推到 main

## 10. 实现顺序

1. **20 min** — forge_refs.py 模块 + API 路由 + 单元测试
2. **10 min** — 前端 Files 视图加 References tab + 路由 + 渲染
3. **10 min** — chip 解析升级（先查 refs → fallback fileRoots）
4. **5 min** — agent_runtime.py 提示词追加
5. **10 min** — smoke + 文档 + 推 main + 等 CI
6. **buffer 5 min** — CI 红了修

## 11. 风险与回退

- **风险**：refs.jsonl 启动回放慢（如果几千条 ref）。**缓解**：本版不优化，几百条内不是问题；下版加 snapshot
- **风险**：MIME 检测不准（用 mimetypes 标准库）。**缓解**：先用扩展名，必要时加 magic number 校验
- **风险**：与正在跑的 v0.7 冲突。**缓解**：v0.7 完全保留，新增并存
- **回退**：如果前端 chip fallback 逻辑复杂，**先只支持 `[[ref:id]]` 一种语法**，agent/label 解析留下版

## 12. 立即解决眼前问题

实现完成后，scott 那条 `[[c3-stability-2026-05-22.md]]` 怎么解决：
- milk agent 下次类似回复前，先调 `POST /api/refs` 注册
- 已经发出去的 chip：让 milk 重新跑一遍场景，或者手动注册一次（脚本即可）
- 长期：milk 的 system prompt 已更新（v0.8 一部分），下次自动注册
