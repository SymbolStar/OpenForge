# OpenForge Branding V2 — 重新校准（多 agent 管理 + 任务分发）

> Designer: Dora 🎨 · Date: 2026-05-25 · Trigger: scott 在 thread 里重新校准初衷

## 这一轮跟 V1 的关键差别

V1 (Ember Forge) 的隐喻是**"多 agent 往中心汇聚"**——alice 当时排的一锚是"汇聚/围炉"。
scott 在 11:06 重新校准：初衷是**"多 agent 管理 + 任务分发"**。

汇聚和分发**方向是反的**：
- 汇聚 = agent → center (产出凝结)
- 分发 = center → agent (任务下派)

V1 三稿全部按"汇聚"语义画，跟 scott 的初衷不一致，因此**整体作废**，重新出 V2。
同时 scott 强调"**简单一眼记住**"——V1 的容器+色块+ember 三层叠加在 favicon 尺寸会糊，V2 必须减到 ≤3 个图元。

## 设计 Token（沿用 V1）

`--forge-bg #0E0F11` · `--forge-fg #F5F5F4` · **`--forge-ember #FF6B35`** · `--forge-glow #FFD8B0`

---

## A · Dispatch Core（推荐主选）— `logo-dispatch-core.svg`

**隐喻**：中心橙圆 = 调度中心（scott / forge 内核），三条白色辐射线 = 任务流，端点白方块 = agent。读法 = "一个中心，向外派活给 N 个 agent"。
**为什么主推**：
1. 直接命中 scott 的初衷（多 agent 管理 + 任务分发），方向感明确（center→out）
2. 三个图元（核 / 线 / 节点）= 足够简单，favicon 16px 仍能识
3. "辐射状"是认知里"调度/dispatch"最强的图形原型（参考：网络中心、路由器图标、CPU 调度示意），无需学习成本
4. 黑底 + 单一橙 accent，跟现 UI 调性零冲突

**短板**：辐射 logo 在某些工具品类已被用过（不限于 dispatch 类），不算原创度最高的形态。

---

## B · Forge F（备选 / 有性格）— `logo-forge-f.svg`

**隐喻**：字母 F 的形态本身就是"主干 + 分支" = 调度树。橙色竖轴 = forge 内核 / 任务主线，两根白色横臂 = 分支派发，臂端的小方块 = agent。底部一点暖色微光暗示"内核常驻在跑"。
**为什么放进来**：
1. F = Forge 首字母 = 直接的品牌锚，记忆负担最低
2. Monogram 形态在 GitHub 头像里有性格（参考 GitHub / GitLab 这类）
3. 形态天然解读为 "trunk + branches"，是任务分发树的隐喻

**短板**：依赖文化语境（要先认出是 F），西方用户友好但中文场景略弱；scott 真要"一眼记住"的话，字母路线对完全不懂英文的人有门槛。

---

## C · Pulse（最克制 / 最现代）— `logo-pulse.svg`

**隐喻**：中心橙点 + 三个等距白点 + 两圈极淡的脉冲环。**不画连线**——靠位置关系让眼睛自动补出"调度关系"。脉冲环暗示"任务正在派发"，做动效时橙点呼吸 + 环向外扩散，是天然的 loading / activity indicator。
**为什么放进来**：
1. 最克制、最有"设计感"——少即是多
2. 天然带动效语义，未来可以做成产品里的 brand-element（loading、agent-busy 指示）
3. 跟 Linear / Vercel / Stripe 这类一流 SaaS 品牌的"极简点阵"美学对齐

**短板**：违反 scott "一眼记住" 的要求——需要观察 + 解释才能读出"分发"语义；不够"自解释"。

---

## 推荐顺序

1. **A Dispatch Core**（最贴初衷，最易记）
2. B Forge F（有品牌字母锚，备选）
3. C Pulse（最美但最不自解释）

## 拍板后我会做的事

- 砍掉未选稿（V1 的四稿一并归档）
- 出 icon-only 16/32/64px favicon 验证
- 出 icon + wordmark 横版（`Open` 白 + `Forge` 橙）
- 出反色版（白底场景用）
- 出 OG image (1200×630)
- 跟 alice 拉一次 tagline 收口

— Dora 🎨

---

## 终稿（2026-05-25 scott 拍板）：B · Forge F

scott 选了 **B · Forge F**。Dispatch Core / Pulse 归档不删，V1 四稿也归档。

### 文件清单（全部在本目录）

| 文件 | 用途 | 备注 |
|---|---|---|
| `logo-forge-f.svg` | icon-only 主稿（256×256） | GitHub avatar、应用图标 |
| `logo-forge-f-16.svg` | 16px 优化版 | favicon 专用，去掉小节点细节 |
| `logo-forge-f-wordmark.svg` | 横版 wordmark | README banner、产品页 hero |
| `logo-forge-f-inverse.svg` | 反色版（浅底） | 印刷物、合作伙伴 deck、light mode |
| `logo-forge-f-og.svg` / `.png` | OG 社交卡（1200×630） | Twitter / Open Graph 分享卡 |
| `logo-forge-f-{16,32,48,256}.png` | favicon PNG 套件 | 网页 `<link rel="icon">` |
| `favicon.ico` | 多分辨率 ICO | 兼容老浏览器 |

### 用法

**GitHub repo avatar**：上传 `logo-forge-f.svg`（或 256×256 PNG）。

**README 顶部**：
```markdown
<p align="center">
  <img src="branding/logo-forge-f-wordmark.svg" alt="OpenForge" width="440">
</p>
```

**网页 favicon**：
```html
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/svg+xml" href="/logo-forge-f-16.svg">
<link rel="apple-touch-icon" href="/logo-forge-f-256.png">
```

**OG card**：用 `logo-forge-f-og.png`（1200×630），tagline 是占位文字，alice 终定后替换。

### 最小留白

icon-only 周围至少留 **半个橙色竖轴宽度**（≈ 13px in 256px viewbox）的安全区，不要让其他元素压到 F 的轮廓外接矩形。

Wordmark 横版左右两端留至少 **一个 F 高度** 的空。

### 禁用做法

- 不要改色（橙色 `#FF6B35` 是品牌核心）
- 不要拉伸、不要旋转、不要描边
- 不要把 F 放在跟 `#FF6B35` 同色系的背景上（橙竖轴会消失）
- 16px 以下场景必须用 `logo-forge-f-16.svg`，不要把 256 版直接缩
- 反色版只能用在浅底，深底必须用主稿

### Tagline（2026-05-25 scott 拍板）

- **EN**（默认 / OG / README / 官网）：`Forge work with agents.`
- **中**（本地化变体，待 scott 二选一）：`和 agent 一起，把活干出来。` 或 `用 agent，锻造你的产品。`

OG 卡主资产用 EN 版（`logo-forge-f-og.png`）；中文本地化变体在 scott 选定后单独 export，不进默认资产。

Trade-off（D-2026-05-25-02）：此 tagline 保 Forge 品牌词双关、舍「调度/分发」叙事；「分发」语义由 logo 形态（F 主干+横臂）自己承载。
