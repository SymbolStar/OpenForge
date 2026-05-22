# OpenForge CI/CD

> 上线日期：2026-05-21（commit `560044d` 起步，`a0c8af8` 收尾全绿）
> 当前最新自动发版：[`v2026.05.21`](https://github.com/SymbolStar/OpenForge/releases/tag/v2026.05.21)

OpenForge 用 GitHub Actions 跑一条「**push 到 main → 跑测试 → 自动打 tag + 发 Release**」的流水线，所有配置在 `.github/workflows/` 下两个文件：

| 文件 | 触发 | 作用 |
|---|---|---|
| `ci.yml` | `push` / `pull_request` → main，以及 `workflow_dispatch` | 跑 lint + shellcheck + 多版本 pytest + 端到端 smoke |
| `release.yml` | `workflow_run`（ci 完成且 success） | 给绿色 main 打日期 tag + cut Release |

## 0. 跑在哪里（Where it runs）

| 维度 | 值 |
|---|---|
| 平台 | **GitHub Actions** |
| 仓库 | [`SymbolStar/OpenForge`](https://github.com/SymbolStar/OpenForge)（public） |
| Runner | `ubuntu-latest`（GitHub 托管，目前是 Ubuntu 24.04），**所有 4 个 job 都跑在 Linux** |
| Minutes | public repo，免费无限 |
| Self-hosted | ❌ 无 |
| macOS / Windows runner | ❌ 无（`bin/forge` 的 launchd 集成是 macOS 限定，目前只靠本地手工验证）|
| Secrets | 仅默认 `GITHUB_TOKEN`，无 PAT / external secret |
| 触发入口 | `push` / `pull_request` → main、`workflow_dispatch`、`workflow_run`（release.yml） |

看跑的情况：

```bash
gh run list --limit 10               # 最近 N 条 run
gh run view <run-id> --log           # 完整日志
gh run view <run-id> --log-failed    # 只看挂掉的 step
gh run watch                          # 实时跟最近一条
```

或者打开浏览器：<https://github.com/SymbolStar/OpenForge/actions>

**⚠️ Runner 上没有 openclaw CLI**：smoke job 跑在 GitHub 干净的 Ubuntu runner 里，没装 openclaw。所以 smoke 建 thread 时正文故意**不带 `@`** —— 一旦带 @，`post_router` 会去 spawn openclaw agent 然后失败。这是 `5421eba` 那次专门绕开的坑。

---

## 1. `ci.yml` — 四个 job

并发控制：`concurrency.group = workflow + ref`，新 push 进来会取消还没跑完的旧 run（PR 推第二次不会浪费一组 minutes）。

### 1.1 `lint` — ruff
- Python 3.13，`pip install ruff` → `ruff check .`
- 项目里的 ruff 规则跟 `pyproject.toml` 走，CI 不重复配置

### 1.2 `shellcheck` — bash 静态检查
- 用 `ludeeus/action-shellcheck@master`
- 默认扫 `./bin/`，外加显式带上 `bin/forge` 和 `tests/fixtures/fake_openclaw.sh`
- **历史坑**：第一版 `560044d` 用了自己写的命令链，CI 上 shellcheck 抓到 `cmd_status` 里一个 SC2015（`A && B || C` 三元式）报错；`a0c8af8` 改成显式 `if/else` 之后全绿

### 1.3 `test` — pytest 矩阵
- 矩阵：Python **3.11 / 3.12 / 3.13** 三档
- `fail-fast: false`，一档红其他档继续跑（方便看「是哪个版本独有问题」）
- `chmod +x tests/fixtures/fake_openclaw.sh` 保证 fixture 可执行（git checkout 后 mode 会被某些 fs 抹）
- **3.11 / 3.12 档**：`python -m pytest tests/ -v`
- **3.13 档**（唯一跑 coverage 的那档）：跟下一节

### 1.3.1 Coverage 门槛 (仅 py3.13)

3.13 档额外跑 `coverage.py`，门槛是 **TOTAL ≥ 80%**，跌破则 job 红。其他档只跑 pytest 以免重复计算。

- 配置：`.coveragerc`
  - `parallel=True`、`concurrency=multiprocessing,thread`
  - `source = agent_runtime, forge_store, post_router, server`
  - `omit = tests/*, .venv/*, web/*`
- 子进程采集：`sitecustomize.py` 检查 `COVERAGE_PROCESS_START` 环境变量，存在则调 `coverage.process_startup()`。`tests/conftest.py` 把仓库根加进 `PYTHONPATH` 让子 Python 能导 `sitecustomize`。这样 `test_server.py` 用 subprocess 起的真 `server.py` 也被覆盖进去（以前 server.py 是 0%）。
- 子进程得收文件：`test_server` 用 `SIGINT` 而不是 `SIGTERM` 去 teardown，开启 server 的 `KeyboardInterrupt` + Python `atexit`（后者是 coverage 写出 `.coverage.*` 文件的点）。

**本地跑一遍**：

```bash
python3 -m pip install coverage pytest --user --break-system-packages
find . -maxdepth 1 -name '.coverage*' -not -name '.coveragerc' -delete
COVERAGE_PROCESS_START=$PWD/.coveragerc \
  coverage run --rcfile=.coveragerc -m pytest tests/ -q
coverage combine --rcfile=.coveragerc
coverage report --rcfile=.coveragerc --fail-under=80
coverage html --rcfile=.coveragerc      # 可选： htmlcov/index.html
```

当前基线（commit `coverage 点举`）：

| 模块 | Stmts | Cover |
|---|---|---|
| agent_runtime.py | 126 | 94% |
| forge_store.py | 545 | 95% |
| post_router.py | 182 | 90% |
| server.py | 438 | 84% |
| **TOTAL** | **1291** | **90%** |

**那些警告**：`Module ... was never imported` 是外层 pytest 进程本身没 import 那几个模块（它们都在子进程里被 fixture 拉起来），combine 后会被子进程的数据覆盖。可以志愿忽略，不影响 `--fail-under` 判断。

### 1.4 `smoke` — 真启服务器走一遍 API
- `needs: [lint, test]`，前两个绿了才跑（节约时间）
- 流程：
  1. `python server.py --port 17878` 后台启动
  2. 轮询 `/api/squads` 最多 5 秒等端口
  3. `POST /api/squads` 建 squad
  4. `POST /api/squads/<id>/threads` 建 thread —— **正文里特意不带 `@`**，避免触发 router（CI 环境没有 openclaw CLI，被 @ 的 agent 调用会挂）
  5. `POST /api/threads/<tid>/posts` 追加一条
  6. `GET /api/threads/<tid>` 校验 `post_count == 2`
  7. kill server，打日志

> **设计选择**：smoke 不复用 pytest，是想验「真 Python 进程 + 真 HTTP + 真 SSE 通道」这条链路。pytest 用的是 in-process test client，覆盖不到 CGI/打包/import-time 的问题。

---

## 2. `release.yml` — 自动发版

### 触发链
```
push → ci.yml 跑完 → workflow_run event (conclusion=success, branch=main) → release.yml
```
**只有 main 上 ci 全绿才会跑 release**，PR 上的绿不会触发（`branches: [main]` 已经过滤）。

### Gate（要不要发版）
不是每条绿 commit 都打 tag。`steps.gate` 读 `git log -1 --format=%s`，按 conventional commit 前缀过滤：

| commit 前缀 | 是否发版 |
|---|---|
| `feat(...)` / `fix(...)` / `perf(...)` / `refactor(...)` … | ✅ 发 |
| `docs(...)` / `chore(...)` / `test(...)` / `ci(...)` | ❌ 跳过 |

判断逻辑：
```bash
if echo "$MSG" | grep -qiE '^(docs|chore|test|ci)(\(|:)'; then
  echo "skip=true"
fi
```

### Tag 命名
- 基础格式：`vYYYY.MM.DD`（UTC）
- 今天已经有同名 tag：追加 short sha → `vYYYY.MM.DD-<sha7>`
- 例：第一个 tag `v2026.05.21`，同一天如果再有发版会变成 `v2026.05.21-a0c8af8`

### 谁来 push tag
```yaml
git config user.name  "openforge-bot"
git config user.email "openforge-bot@users.noreply.github.com"
```
Release notes 用 `gh release create --generate-notes` 让 GitHub 自动从 commit / PR 历史抓。

### 权限
- `permissions: contents: write` —— 这个 workflow 唯一需要的额外权限
- 用的是默认 `GITHUB_TOKEN`，**没有任何 PAT / external secret**

---

## 3. 关键状态信号

| 信号 | 怎么看 |
|---|---|
| 最近一次 CI 状态 | `gh run list --workflow=ci --limit 5` |
| 最新自动发版 | `gh release list --limit 1` |
| 单条 commit 的发版决策 | release run logs 里 `→ skipping release for ...` 一行 |
| CI 历史失败原因 | `gh run view <run-id> --log-failed` |

---

## 4. 已知局限 / 下一步

1. **smoke 用的是 happy path**：没覆盖 reactions / reply nesting / SSE consumer。下一轮可以加一条 SSE 订阅 + 校验 event sequence
2. **没有 Windows / macOS runner**：服务端 only-on-Linux 暂时够用，但 `bin/forge` launchd 集成是 macOS 限定，目前完全靠手工本地验证
3. **release notes 模板**：现在是 GitHub 自动 generate，没有手写 changelog；如果以后要面向用户发布 binary，需要换 release-please 之类的方案
4. **没有部署环节**：OpenForge 现在是本地服务（127.0.0.1:7878），无需 deploy。等真正要 host 时再加 `deploy.yml`

---

## 5. 改 CI 的时候记一下

- 任何 workflow 改动，**先在 fork / branch 上 `act` 或 `workflow_dispatch` 验**，别直接 push main（main 红 = release.yml 不跑 = 发版静默卡住）
- 改 `release.yml` 的 gate 正则时，记得用 `grep -qiE`（i = ignore case，E = extended regex），手写测一遍 `docs:` / `docs(prd):` / `chore: x` / `feat(x):` 四个情况
- ruff / pytest 升级最好跟着 `requirements*.txt` 一起 pin，避免「昨天绿今天红」的 transitive 飘移
