# OpenForge Branding V1 — Logo 提案

> Designer: Dora 🎨 · Date: 2026-05-24 · Scope: 第一轮 logo 实稿，scott 二选一拍板

## TL;DR
两个方向、四个文件。**方向一 Ember Forge 是推荐主选**（贴合 alice 排的"汇聚"一锚 + 现 UI 中性黑调）；方向三 OF Monogram 作锻印一锚的对照锚。看实物拍板，文字解释见下。

## 设计 Token

| Token | 值 | 用途 |
|---|---|---|
| `--forge-bg`     | `#0E0F11` | 主背景（比纯黑略暖，跟现 UI 中性黑一脉） |
| `--forge-fg`     | `#F5F5F4` | 主前景（off-white，避免纯白刺眼） |
| `--forge-mute`   | `#9CA3AF` | tagline / 次级文字 |
| **`--forge-ember`** | **`#FF6B35`** | **暖色 accent — 余烬橙**，Pantone 接近 165 C |
| `--forge-ember-glow` | `#FFD8B0` | ember 中心高光 |
| `--forge-copper` | `#B8531C` | 锻印方向用的深铜色（方向三专用） |

色值选择理由：`#FF6B35` 是"余烬"而不是"火焰"——比纯红更暖、比纯橙更沉，黑底上有发光感但不刺眼；跟 GitHub README、Linear/Vercel 这些工程师常驻平台的深色 UI 都能共存而不打架。

---

## 方向一 · Ember Forge（推荐）

**隐喻**：方形/六边形容器 = thread / 锻造炉，内部小色块 = 多 agent 各自的发言/产出单元，中心橙点 = 协作凝结出的成果（"余烬"，余温暗示"刚锻好"）。
**回应**：alice 一锚（汇聚）+ 现 UI 中性黑调 + 有温度不冷漠。

### 变体 1.1 — `logo-ember-square-3.svg`
方形容器 + 3 个 agent 色块。**主推 GitHub repo avatar**——方形容器跟 GitHub 头像的圆角方形 mask 天然契合；3 个色块够"多人协作"语义又不至于挤。

### 变体 1.2 — `logo-ember-hex-4.svg`
六边形容器 + 4 个 agent 色块。**备选**——hex 在工程师群体里有 "Hashicorp / Honeycomb" 那种生产工具系的血统，4 个色块语义更"多 agent"，但 hex 在小尺寸（favicon 16/32px）下识别度比方形稍弱。

### 变体 1.3 — `logo-ember-wordmark.svg`
icon + wordmark 横版。**用于 README banner / 产品页 hero / OG image**。`Open` 用 fg 色，`Forge` 用 ember 橙，下方一行 tagline `THREAD-SHAPED · AGENT-NATIVE`（这行是 placeholder，scott 拍方向后让 alice 终定 tagline）。

---

## 方向三 · OF Monogram（对照锚）

### `logo-monogram-of.svg`
**隐喻**：O 是锻造环、F 的横臂像一根穿过 O 的钢条，右端探出环外有铜橙色"刚出炉"的余热点——整体读作一枚 forge mark（锻造厂的钢印）。底部两个小点是印章四角的 stamp dots 暗示。
**调性**：有工业血统但通过几何抽象避开了"铁砧锤子"的土味，类似 GitHub Octocat 那种"工具有性格"的感觉。
**风险**：monogram 路线在小尺寸下需要再调，并且强调的是"产出物有出处"——alice 的产品定位里这是二锚，所以放在这里作为对照而不是主推。

---

## 推荐用途矩阵

| 场景 | 推荐文件 |
|---|---|
| GitHub repo avatar (1:1) | `logo-ember-square-3.svg` |
| README header banner | `logo-ember-wordmark.svg` |
| favicon 16/32px | `logo-ember-square-3.svg`（去内部色块只留容器+ember） — 后续单独出 |
| 产品页 hero | `logo-ember-wordmark.svg` |
| 对照备选 | `logo-monogram-of.svg` |

---

## 拍板需要的两个决定

1. **方向**：Ember Forge（推荐）vs OF Monogram（对照）
2. **如果选 Ember**：方形 3 块 vs 六边形 4 块

scott 拍完之后我会：
- 砍掉未选稿
- 出 favicon / OG image / dark-on-light 反色版
- 跟 alice 拉一次 tagline 终定（如果走 wordmark）
- 让 codex 把选定 SVG 接进 OpenForge UI 的 header

— Dora 🎨
