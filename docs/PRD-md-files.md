# PRD: OpenForge Files 与 Markdown 编辑器

> 版本：v0.6-files-md
> 日期：2026-05-22
> 负责人：Scott（产品）/ Claude Code（实现）/ Judy（监督）
> 目标交付窗口：**1 小时内 commits push 到 main + CI 全绿**

---

## 1. 背景与动机

OpenForge 当前是 multi-agent topic tracker，左侧只有 Squads 一列。我们要把它变成一个更完整的工作面板：
- **沉淀产物**（PRD / TODO / 设计文档 / 会议纪要）以 Markdown 文件形式集中存放
- **左侧栏改成图标式导航**（类似 Slack 的 Home/DMs/Activity/Files/Later/More），把不同入口区分开
- **Files 入口**进入 Markdown 文件浏览器，支持预览 + 编辑 + 保存

## 2. 范围

### ✅ In scope（这一版必须做）
1. **左侧 icon rail**（最左一列，宽度约 72px）：
   - Home（默认，回到 squads 视图）
   - Files（新功能，本 PRD 重点）
   - 视觉风格参考截图：深色背景、白色图标 + 文字标签、当前选中态高亮
2. **Files 主视图**：
   - 左中栏：文件树 / 列表（展示根目录下的 `.md` 文件）
   - 右栏：预览模式（默认）+ 编辑模式（点「编辑」切到 textarea）
3. **Markdown 渲染**：使用 [marked.js](https://cdn.jsdelivr.net/npm/marked/marked.min.js) CDN，无需后端渲染
4. **基础 CRUD**：
   - 列出所有 md 文件
   - 读取单个 md 文件
   - 保存（覆盖）已有 md 文件
   - 新建 md 文件
5. **CI 全绿**：所有改动通过现有 lint + shellcheck + pytest + smoke + coverage (≥80%) gate

### ❌ Out of scope（这一版不做）
- 文件夹 / 子目录嵌套（只支持平铺单层）
- 文件删除、重命名、移动（下一版加）
- 非 md 文件类型（pdf / png / docx 等）
- 协作编辑 / 实时同步
- 版本历史 / 撤销
- DMs / Activity / Later / More 这些导航项的实际功能（**只占位显示图标**，点击 toast 提示「敬请期待」）

## 3. 用户故事

- **U1**：作为 Scott，我打开 OpenForge → 看到左侧多了一条图标导航栏 → 点 Files → 看到我之前在 OpenForge 文件夹里写的所有 md 文件
- **U2**：点击一个文件名 → 右侧渲染出格式化好的 markdown（标题、列表、代码块、表格）
- **U3**：点「编辑」按钮 → 切换到带 textarea 的编辑模式 → 改完点「保存」→ 文件持久化 → 自动回到预览模式
- **U4**：点「+ 新建」→ 弹出 prompt 让我输文件名（必须 `.md` 结尾）→ 创建空文件 → 自动进入编辑模式

## 4. UI 规格

### 4.1 整体布局

```
┌──────┬───────────┬──────────────┬─────────────────────┐
│ icon │ squad-rail│ thread-rail  │   thread-pane       │ ← Home 视图（现有）
│ rail │ (现有)    │  (现有)      │   (现有)            │
└──────┴───────────┴──────────────┴─────────────────────┘

┌──────┬─────────────────┬─────────────────────────────┐
│ icon │ files-rail      │   files-pane                │ ← Files 视图（新）
│ rail │ (md 文件列表)   │  (预览 / 编辑切换)          │
└──────┴─────────────────┴─────────────────────────────┘
```

### 4.2 Icon Rail 详细规格

- **宽度**：72px（固定，不可拖动）
- **背景**：`#1a1d24`（比 squad-rail 略深）
- **顶部 logo**：可保留 OpenForge 🔨 emoji 或一个 home icon
- **导航项**（从上到下）：
  | 图标 | 标签 | 状态 |
  |---|---|---|
  | 🏠 (`home`) | Home | ✅ 激活：回 squads 视图 |
  | 📁 (`folder`) | Files | ✅ 激活：进入 files 视图 |
  | 💬 (`message`) | DMs | 灰色，点击 toast「敬请期待」|
  | 🔔 (`bell`) | Activity | 灰色，点击 toast「敬请期待」|
  | 🔖 (`bookmark`) | Later | 灰色，点击 toast「敬请期待」|
  | ⋯ (`more`) | More | 灰色，点击 toast「敬请期待」|
- **激活态**：图标 + 文字加亮（白色），背景柔和高亮（`#2a2f3a`）
- **图标实现**：用 unicode emoji 即可（不引入 icon font）；或者 SVG inline

### 4.3 Files Rail（文件列表）

- 顶部标题 `FILES` + 「+ 新建」按钮
- 列表项：文件名（去掉 `.md` 后缀显示，hover 显示完整名 + 修改时间）
- 当前选中项高亮（蓝色侧边条 + 浅背景）
- 空状态：「还没有 md 文件，点上面 + 新建一个吧」

### 4.4 Files Pane（预览/编辑）

- **顶部 toolbar**：
  - 左：文件名 + 大小 + 修改时间
  - 右：「预览 / 编辑」切换按钮 + 「保存」按钮（编辑模式才显示，未改动时禁用）
- **预览模式**：marked.js 渲染 markdown，样式简洁（github-light 风格，复用 OpenForge 现有 typography）
- **编辑模式**：全屏 textarea，等宽字体，自动 grow
- **键盘**：编辑模式下 `Cmd/Ctrl+S` 触发保存
- **未保存提示**：编辑过未保存就切走/刷新，浏览器原生 `beforeunload`

## 5. 后端 API 设计

### 5.1 存储位置

- 根目录：`~/.openclaw/openforge/files/`
- 不存在则自动创建
- 只允许文件名是 `[A-Za-z0-9_-]+\.md`（防路径穿越；不允许 `/`、`..` 等）

### 5.2 路由

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/api/files` | — | `{files: [{name, size, mtime}]}` |
| `GET` | `/api/files/<name>` | — | `{name, content, mtime}` |
| `PUT` | `/api/files/<name>` | `{content}` | `{name, size, mtime}` |
| `POST` | `/api/files` | `{name, content?}` | `{name, size, mtime}` (201) |

### 5.3 错误处理

- 文件名非法 → 400 `{"error": "invalid filename"}`
- 文件不存在 → 404
- 文件已存在（POST）→ 409
- 写入失败 → 500

## 6. 前端实现要点

1. **路由**：用 URL hash（`#/squads` / `#/files` / `#/files/<name>`）切视图
2. **状态**：当前视图 + 当前选中文件 维护在简单的 `state` 对象里
3. **样式**：复用 `style.css` 现有变量（颜色、字号、间距），新加 `.icon-rail` / `.files-rail` / `.files-pane` 等 selectors
4. **marked.js**：CDN 引入即可，不依赖打包工具
   ```html
   <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
   ```
5. **保留兼容**：Home 视图（squads + threads + posts）所有行为不变

## 7. 测试要求

### 7.1 Pytest 测试（必须）
- `tests/test_files.py`：
  - GET /api/files 返回列表（empty + 多文件）
  - GET /api/files/<name> 读取已有文件
  - POST 新建 → 返回 201 + 文件存在
  - PUT 覆盖 → 内容更新
  - 文件名非法（`../etc/passwd`、`foo.txt`、`foo`、空字符串）全部 400
  - 重复 POST 同名 → 409
  - 不存在的 GET / PUT → 404

### 7.2 Coverage
- 整体仍需 ≥80%（Judy 在跑的 codex 任务正在拉这个数字）
- 本功能新加代码必须 100% 覆盖（小模块好做到）

### 7.3 Smoke
- `.github/workflows/ci.yml` 的 smoke job 增加：
  - POST 一个 md 文件
  - GET 列表确认在
  - PUT 改一遍内容
  - GET 内容比对
  - 不带 @ 内容，避免触发 router

## 8. 验收 Checklist

- [ ] 左侧 icon rail 出现，6 个导航项，Home/Files 可用，其他 toast
- [ ] 文件列表正确展示 `~/.openclaw/openforge/files/*.md`
- [ ] 点击文件 → 预览渲染 markdown
- [ ] 编辑 → 改内容 → 保存 → 刷新后还在
- [ ] 新建文件流程通畅
- [ ] 路径穿越攻击被拦
- [ ] pytest 全过，coverage ≥80%
- [ ] CI 全绿（lint + shellcheck + pytest 矩阵 + smoke + coverage gate）
- [ ] 多条 conventional commit 推到 main：
  - `feat(api): files CRUD endpoints`
  - `feat(web): icon rail navigation`
  - `feat(web): markdown files preview/edit`
  - `test(files): full coverage`
  - `docs: PRD-md-files`

## 9. 实现顺序建议

1. **30 分钟**：后端 API + 测试（最关键，单元测好做）
2. **20 分钟**：前端 icon rail + files view（HTML/CSS/JS）
3. **10 分钟**：smoke 路径 + 文档 + 推 main + 等 CI

## 10. 风险与回退

- **风险**：与 Judy 在跑的 coverage→80% codex 任务有 merge 冲突。**缓解**：本 PRD 不动 reactions/router/store，只加新文件 + 改 `server.py` 路由表（追加，不改既有路由）+ 加新前端文件 / 在 `web/*` 追加；冲突面小
- **回退**：如果 1 小时内交付不完，**先把后端 API + 测试 push（feat(api) commit）**，前端拆下一轮；不要带半残的 UI 进 main
