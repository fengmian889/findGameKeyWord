# Poki SEO Monitor

Poki SEO Monitor 用于监控 Poki 英文游戏目录中新发布的游戏，并面向全球英语市场生成有证据支撑的 SEO 调研结果，默认以美国作为首要信号地区。

监控器同时从 Poki sitemap 和 New Games 页面发现规范化的 `https://poki.com/en/g/<slug>` URL，提取页面事实，生成游戏名称词、品类/玩法词和长尾关键词候选，并通过有请求上限的公开信号进行评估。系统会写入持久化的 JSON、JSONL、CSV 和 Markdown 产物，并可为需要关注的机会创建具备幂等性的 GitHub Issue。

这是一个调研监控工具。它不会发布 SEO 页面，不会声称提供绝对搜索量或权威关键词难度，不会递归爬取整个 Poki，也不会绕过访问控制。

## 市场范围与评分

监控目录限定为英文站（`/en/`）。信号采集默认使用 `TRENDS_GEO=US`；应将结果视为面向全球英语 SEO 审查的“美国优先”证据，而不是对所有国家同时进行的测量。如需比较特定市场，请为其他 Trends 地区使用独立的状态和输出路径运行。

每个关键词都会获得 0–100 分的**趋势机会评分（Trend Opportunity Score）**，权重如下：

- 趋势强度与增长：30%
- 新鲜度：25%
- 竞争缺口：20%
- 搜索意图证据：15%
- 长尾扩展潜力：10%

建议动作分为 `immediate`（75–100）、`watch`（55–74）、`hold`（35–54）和 `ignore`（0–34）。置信度会单独输出；当数据提供方证据不足时，它会限制建议动作的最高等级。评分是用于相对比较的调研信号，不是搜索量估算。

新鲜度来自持久化的发现证据；生产环境不会为每次调研都固定赋值 `1.0`。时间因素会在 30 天内线性衰减至零。在发现当天，证据因子为 `0.60 + 0.25 × New Games rank + 0.15 × source timing`：排名值从第 1 位的 `1.0` 衰减到第 51 位的零；双来源时间值在 New Games 和 sitemap 同时首次发现该 URL 时为 `1.0`，当时间差达到 7 天时衰减为零。仅由 New Games 发现时，来源分量使用 `0.75`；仅由 sitemap 发现时使用 `0.25`；旧版或其他发现记录使用 `0.50`。证据因子再乘以时间因子，因此每次 7/14/30 天复查的新鲜度都会低于此前的调研。

## 环境要求

- Python 3.12 或更高版本
- Git
- Google Chrome，或与当前 `trendspyg` Google Trends 适配器兼容的浏览器环境
- 能够通过 HTTPS 访问 Poki、Google autocomplete/Trends、DuckDuckGo HTML 搜索，以及可选的 GitHub API

定时工作流使用 Ubuntu 和 Python 3.12。本地运行的平台需要支持 POSIX 风格文件锁；如果无法实现安全的跨进程锁定，报告层会以失败关闭（fail closed）的方式停止。

## 本地安装

创建虚拟环境，安装锁定的依赖集合及项目包：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
```

开发时也可以使用声明的开发依赖进行可编辑安装：

```bash
.venv/bin/python -m pip install -e '.[dev]'
```

运行测试：

```bash
.venv/bin/python -m pytest -q
```

在本地运行监控器但不创建 GitHub 通知：

```bash
.venv/bin/poki-seo-monitor --dry-run
```

命令会向标准输出打印一条 JSON 摘要。发生致命错误时，会向标准错误输出经过脱敏的 JSON，并以状态码 1 退出。

## 配置

配置通过环境变量读取。

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `SITEMAP_INDEX` | `https://poki.com/en/sitemaps/index.xml` | 用于权威发现的 sitemap 索引 |
| `NEW_GAMES_URL` | `https://poki.com/en/new` | 用于提前发现和故障回退的 New Games 页面 |
| `TRENDS_GEO` | `US` | Google Trends 的首要地区；同时兼容旧变量 `GEO` |
| `STATE_PATH` | `data/state.json` | 持久化 URL 状态和复查计划 |
| `REPORTS_DIR` | `reports` | Markdown 报告根目录 |
| `GAMES_PATH` | `data/games.jsonl` | 结构化游戏调研历史 |
| `KEYWORDS_PATH` | `data/keywords.csv` | 每个已调研游戏 URL 的最新关键词级表格 |
| `MAX_GAMES_PER_RUN` | `10` | 单次运行中新游戏调研和复查的正整数上限 |
| `BASELINE_SAMPLE_SIZE` | `3` | 新基线建立时抽样的当前游戏数量，同时受 `MAX_GAMES_PER_RUN` 限制 |
| `GITHUB_REPOSITORY` | 未设置 | 用于查询/创建 Issue 的目标仓库，格式为 `owner/repo` |
| `GITHUB_TOKEN` | 未设置 | 用于查询/创建 GitHub Issue 的 Token |

实验或不同地区比较应使用不同的状态和输出路径。相对路径以当前工作目录为基准解析。

## 基线规则

系统通过 `STATE_PATH` 是否存在来判断本次运行是否为基线运行。

第一次成功运行时，如果状态文件不存在，监控器会：

1. 发现并记录当前可见的所有规范化游戏 URL，作为基线；
2. 只调研少量样本，并优先选择出现在 New Games 页面中的游戏；
3. 禁止为这些样本创建任何 GitHub 通知；
4. 返回包含 `"baseline": true` 和 `"new": 0` 的摘要。

这样可以避免把整个历史目录误判为新发布游戏。下一次运行时，`"baseline"` 为 false。已有的基线 URL 不会作为新游戏重新调研；只有新发现的 URL、到期复查项、失败调研重试或待处理的 Issue 通知才符合处理条件。

正常运行期间不要删除 `data/state.json`。删除该文件或修改 `STATE_PATH` 会有意创建新基线，并丢失 URL 级调度信息和 Issue 引用。如确需重建，请先备份该文件。

即使成功发现的 URL 数量为零，系统仍会创建基线状态。如果两个发现来源都失败，监控器会在不创建或改写状态的情况下失败，因此网络中断不会建立一个空基线。

## dry-run 与安全冒烟测试

`--dry-run` 表示**不创建 GitHub Issue 通知**。即使环境中存在 `GITHUB_TOKEN`，它也会从运行时配置中移除已配置的 GitHub Token。

为保障子进程和数据提供方进程的安全，CLI 在构建和运行监控器期间还会临时从进程环境中移除 `GITHUB_TOKEN`，并在返回给进程内调用者前恢复原值。即便有此保护，执行隔离冒烟测试时仍应显式取消 GitHub 凭据，以免无关的 Shell 包装器或诊断工具继承凭据。

dry-run 不是离线、只读或禁止写入模式。它仍然会：

- 实时请求发现页面、游戏页面和公开信号数据提供方；
- 创建或更新配置的状态文件；
- 写入 JSONL、CSV 和 Markdown 输出；
- 当基线抽样进入 Google Trends 阶段时，可能运行数分钟。

安全的实时冒烟测试应使用全新且明确的临时路径：

```bash
env -u GITHUB_TOKEN -u GITHUB_REPOSITORY \
STATE_PATH=/tmp/poki-seo-smoke-state.json \
REPORTS_DIR=/tmp/poki-seo-smoke-reports \
GAMES_PATH=/tmp/poki-seo-smoke-games.jsonl \
KEYWORDS_PATH=/tmp/poki-seo-smoke-keywords.csv \
.venv/bin/poki-seo-monitor --dry-run
```

使用完全相同的命令再运行一次。第一次成功结果应包含 `"baseline": true`；第二次应包含 `"baseline": false`、`"new": 0`，并且通常为 `"processed": 0`，除非两次运行之间出现了新 URL 或到期项目。检查临时状态以确认已知 URL 被保留，并比较 JSONL、CSV 和报告数量，确认已有基线 URL 没有被当作新调研重复写入。

此冒烟测试不需要 `GITHUB_TOKEN` 或 `GITHUB_REPOSITORY`。如需进行完全独立的新尝试，请使用新的路径名称。

## GitHub 仓库设置

工作流定义在 [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml)。

1. 为仓库启用 GitHub Actions。
2. 启用仓库 Issues。
3. 在 **Settings → Actions → General → Workflow permissions** 中，允许 GitHub Actions 读取和写入仓库内容。
4. 保留 `monitor` job 中限定范围的 `contents: write` 和 `issues: write` 权限。测试 job 继续保持只读。
5. 确保分支保护规则允许 GitHub Actions bot 推送生成的 `data/` 和 `reports/` 变更，或配置等效的受审查集成方式。

工作流使用当前运行内置的 `github.token`，不需要个人访问 Token。不要把名为 `GITHUB_TOKEN` 的个人 Token 写入仓库文件。未来付费数据提供方的凭据应保存在 GitHub Actions Secrets 中，并且只暴露给真正需要它的步骤。

工作流会从 `requirements.lock` 安装经过哈希验证的依赖，运行完整测试，然后执行监控器。产物发生变化时，它只提交 `data/` 和 `reports/`，在当前分支上执行 rebase，并最多重试三次经过身份验证的 push。并发组会阻止监控任务重叠运行。

## 定时与手动运行

GitHub Actions 每六小时在第 17 分钟运行一次监控器：

```text
17 */6 * * *
```

GitHub cron 使用 UTC，因此通常会在 UTC 00:17、06:17、12:17 和 18:17 左右运行。GitHub 负载较高时，定时任务可能延迟。

如需手动运行，请打开 **Actions → Poki SEO Monitor → Run workflow**，选择目标分支并确认运行。如果 GitHub CLI 已完成身份验证，也可以通过相同的 `workflow_dispatch` 触发器启动：

```bash
gh workflow run monitor.yml
```

在处理 Issue 前，请检查 **Monitor Poki** 步骤中的 JSON 摘要以及生成的 commit。

## 输出

- `data/state.json` 是以规范化 URL 为键的可变运行状态。它记录首次发现时间、发现来源、各来源的首次发现时间戳、观测到的最佳 New Games 排名、调研/重试状态、趋势复查、报告位置和 Issue 结果。缺少可选溯源字段的 schema-v1 旧状态仍可加载，并使用保守且随时间衰减的回退值。
- `data/games.jsonl` 包含经过严格校验和版本化的调研记录，其中包括发现溯源、首次发现时间、页面事实、候选词证据、原始数据提供方信号、评分、置信度、动作、数据提供方错误、复查状态/计划、报告引用和初始 Issue 处置结果。运行时写入 v2 `research` 记录以及仅追加的 v2 `issue_outcome` 事件，因此最终的创建/失败结果可以持久保存，而无需重写不可变调研历史。现有 v1 调研记录仍然有效，也能用于通知重试。后续复查可以添加新的调研记录；完全重复的记录不会重复追加。
- `data/keywords.csv` 是适合筛选或导入电子表格的关键词级表格。某个已调研游戏 URL 的行会被其最新结果替换。类似公式的单元格前缀会被转义。
- `reports/YYYY-MM-DD/<slug>.md` 是便于人工审阅的证据报告。它会区分提取的页面事实、生成的候选词、观测信号、数据提供方错误和评分推断。

产物发布使用锁和日志机制，因此 Markdown、JSONL 和 CSV 会作为一组完成更新，或在中断后恢复。已有产物格式错误、路径不安全、目标是符号链接或目标发生冲突时，系统会失败，而不是静默覆盖。

运行摘要包含：

- `baseline`：本次运行是否建立了状态基线；
- `discovered`：所有成功发现来源中可见的规范化 URL 数量；
- `new`：基线之后首次发现的 URL 数量；
- `processed`、`completed` 和 `failed`：受上限约束的调研结果；
- `notification_retried`：使用已保存报告重新尝试 Issue 通知的数量；
- `degraded`：是否发生来源、数据提供方或发布错误；
- `errors`：经过脱敏且长度受限的诊断信息。

在基线之后的调研中，最高分不低于 75 的每个游戏都会获得一个独立的高优先级 Issue。同一次运行中最高分为 55–74 的游戏会合并到一个普通机会摘要 Issue 中。如果一个游戏低于 55 分，但所有已尝试的首要关键词都缺少 7 天 Trends 数据，它也会加入该摘要，以免所需的 7/14/30 天复查被静默遗漏。基线样本绝不通知；如果部署环境缺少任一 GitHub 配置值，系统会记录 `not_configured`，而不会留下永久重试。每个 Issue 正文都包含由 URL 派生的稳定标识（摘要中的每个成员也有标识）；批次标识、持久状态、工作流并发控制和 GitHub 搜索共同降低重复概率。调研产物和待通知状态会在调用 Issue API 前保存。发生部分失败时，只有失败的 URL/分组保持待处理；下一次运行仅使用已保存的 JSONL/报告产物重建 Issue，不会重新抓取页面或信号数据提供方。GitHub 搜索不是事务级的 exactly-once 保证，因此运维人员仍应检查异常重试场景。

## Google Trends 限制与降级行为

Google 官方 Trends API 仍处于受限 Alpha 阶段，不能假定每个部署都能使用。当前实现固定使用 `trendspyg==1.1.1`；这是读取公开 Trends 页面体验的非官方第三方适配器/客户端，不是 Google 官方 API，也不是 Google 官方支持的客户端。

这会带来以下运行限制：

- 每个关键词都使用 `today 3-m` 时间范围单独查询。报告中的 7、30 和 90 天数值，是根据该次查询的相对兴趣序列计算出的平均值。
- Trends 数值是每次独立查询内部的 0–100 相对兴趣指数，不是绝对月搜索量。由于每条序列可能独立缩放，因此不同关键词的独立查询结果不能可靠地直接横向比较。
- 公开页面体验及其浏览器自动化速度较慢，容易受到速率限制，也可能因上游 UI 变化而失效。
- 单次 Trends 查询的截止时间为 90 秒，并在隔离进程中运行，因此可以终止卡住的浏览器。
- 为控制请求量，每个游戏只有前三个候选词会请求 Trends；最多十个候选词请求 autocomplete；只有第一个候选词请求受限的公开 SERP 信号；最多为二十个候选词评分。生产环境中共享 session 的 autocomplete 和 DuckDuckGo 请求使用容量受限的 TTL/LRU 缓存，并分别以 0.25 秒和 1 秒的间隔串行启动未缓存请求。
- Trends 数值为零与 Trends 数据不可用是两种情况。缺失数据会被表示为缺失、降低置信度，并且绝不会被描述为实测的零需求。

各数据提供方的失败相互隔离。Trends 失败时，autocomplete 和受限的 DuckDuckGo HTML SERP 信号仍会继续；错误会记录在报告中。监控器不会激进重试、轮换身份、破解 CAPTCHA、通过重定向规避控制或绕过封锁。

如果所有已尝试的首要关键词都缺少 7 天 Trends 数据，该游戏会被安排在第一次成功调研后的第 7、14 和 30 天复查。每个计划时间到期后只消费一次。成功获得 Trends 结果会清除剩余复查；持续缺少数据时会保留已经建立的计划，而不会无限添加日期。页面或调研失败使用独立的次日重试路径。

排查 Trends 问题时，首先确认执行环境能够启动 Chrome，然后检查报告中的数据提供方错误。重复出现 429、同意/插页页面、上游标记结构变化或浏览器启动失败时，应视为上游降级。请等待数据提供方恢复或替换适配器；不要削弱超时、响应大小限制、TLS 要求或访问控制保护。

## 未来接入 DataForSEO 或 Ahrefs

付费数据提供方应在信号提供方边界后接入，不应修改发现、规范化 URL 状态、页面提取、关键词生成或报告编排。

安全的迁移步骤如下：

1. 实现数据提供方适配器，统一搜索量、CPC、关键词难度、SERP 特征和竞争域证据；
2. 仅从环境变量或 GitHub Actions Secrets 加载凭据；
3. 保留缺失值和数据提供方特有错误，不虚构默认值；
4. 在写入新字段前，明确扩展结构化信号 schema 并提升版本；
5. 增加评分模型版本，使免费/公开数据与付费数据提供方评分都能得到解释；
6. 在工作流中启用实时调用前，先使用 fixture 添加契约测试。

获得 Google Trends 官方 API 访问权限后，也可以通过相同边界替换当前 Trends 数据提供方。替换不应要求重写发现、状态或报告模块。

## 故障排查

### 命令以 `discovery failed` 退出

sitemap 发现和 New Games 页面均失败。请检查出站 HTTPS、DNS、Poki 可用性以及配置的 URL。系统会有意保持状态不变。如果只有一个来源失败，本次运行会继续，并返回 `"degraded": true` 和经过脱敏的来源错误。

### 第一次运行调研了很多历史游戏

确认每次调用使用相同的 `STATE_PATH`。状态路径不存在、被删除或解析到不同位置时，会启动新的基线。`BASELINE_SAMPLE_SIZE` 只控制基线调研数量；所有发现的 URL 仍会被记录，确保后续不会被视为新游戏。

### dry-run 仍然创建了文件

这是预期行为。dry-run 只禁用 GitHub Issue 创建。当你不希望修改仓库产物时，请把四个输出变量全部指向隔离目录或 `/tmp`。

### Trends 很慢或没有数据

确认浏览器可用，并读取 JSONL 中的 `signals.errors` 或 Markdown 中的 **Provider errors**。Trends 缺失会降低置信度并安排复查。不要把它解释为零兴趣，也不要循环执行命令来强制获得结果。

### 工作流无法创建 Issue

确认仓库已启用 Issues，并且 monitor job 保留 `issues: write`。在本地进行非 dry-run 运行时，同时设置 `GITHUB_REPOSITORY=owner/repo` 和具有 Issue 权限的 Token。发布失败时，调研产物会保留，通知会标记为待处理，以便稍后重试，而不会重新抓取页面或数据提供方。

### 工作流无法推送生成的数据

确认 `contents: write`、仓库工作流权限和分支保护策略。工作流会有意在 checkout 时不持久化凭据，并且只在 fetch/push 时安装临时身份验证 Git header；随后会清理该 header。

### 输出文件被报告为损坏或不安全

不要用空文件替换它。请保留状态和产物，检查准确的严格校验错误，并从 Git 历史恢复最后一个已知正常版本。写入器会拒绝格式错误的 JSONL/CSV、符号链接目标、路径逃逸、目标不匹配和损坏的恢复日志，避免进一步破坏数据。

### 运行结果为 degraded，但进程成功退出

这表示流水线已完成足够多的步骤并保留了有用结果，但某个发现来源、信号数据提供方或通知失败。请检查 `errors`、报告和状态，再决定是否需要干预。致命的配置错误、发现来源全部失败、状态校验失败或不安全发布错误会以非零状态退出。

## 安全与负责任运行

- 数据提供方凭据只能保存在 GitHub Actions Secrets 或本地进程环境中。绝不要提交 `.env` 文件、Token、浏览器配置文件或 Cookie。
- 优先使用内置的短期 GitHub Actions Token，并保留最小权限的 job 配置。
- 本地冒烟测试使用 `--dry-run`；它会从运行时配置中移除 `GITHUB_TOKEN`，并在输出前对致命错误进行脱敏。
- 所有受监控内容都属于不受信任的外部输入。报告会转义 Markdown 表格内容，CSV 输出会保护类似公式的单元格，Issue 输入也有大小和格式限制。
- Poki sitemap、New Games 和游戏页面请求仅使用 HTTPS，并设置明确超时、有上限的重试、响应大小限制和声明的 User-Agent。
- Google autocomplete 和 DuckDuckGo HTML 信号请求设置明确超时、禁用重定向、采用顺序/保守节奏，并执行严格的单次运行请求预算，但目前没有强制响应字节上限。Google Trends 使用隔离且受截止时间限制的浏览器自动化进程，不经过 Poki 请求路径。
- 不要为了绕过速率限制而增加请求量，也不要绕过登录、CAPTCHA、同意页面、robots 或其他访问控制。站点提出要求时，应暂停实时运行。
- 生成的报告属于需要人工审查的调研证据。外部数据提供方可能不完整、过时、个性化或暂时不可用。
- 仓库输出可能暴露已调研的游戏/关键词数据。请根据组织要求选择适当的仓库可见性和保留策略。

有关架构、状态行为、评分依据和验收标准，请参阅 [`docs/superpowers/specs/2026-08-03-poki-seo-monitor-design.md`](docs/superpowers/specs/2026-08-03-poki-seo-monitor-design.md)。
