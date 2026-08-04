# README 中文本地化执行计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将项目根目录的英文 `README.md` 完整翻译为简体中文，同时保持所有命令、配置、链接与技术语义不变。

**Architecture:** 这是纯文档本地化任务。先记录不可翻译的技术标识与 Markdown 结构，再进行等结构全文翻译，最后通过文本扫描、围栏检查和完整测试验证未引入信息或格式回归。

**Tech Stack:** Markdown、ripgrep、Python 3.12、pytest。

---

### Task 1: 记录翻译前结构与技术标识

**Files:**
- Read: `README.md`

- [ ] **Step 1: 记录 Markdown 结构和代码围栏数量**

Run:

```bash
rg -n '^(#|```|\| `|\[)' README.md
```

Expected: 输出全部标题、代码围栏、配置表行和链接，作为翻译后核对基线。

- [ ] **Step 2: 记录必须保持原样的关键标识**

Run:

```bash
rg -o '`[A-Z][A-Z0-9_]+`|https://[^ )]+|[0-9]+ \*/[0-9]+ \* \* \*' README.md | sort -u
```

Expected: 包含 `TRENDS_GEO`、`STATE_PATH`、`GITHUB_TOKEN`、Poki URL 和 cron 表达式。

### Task 2: 完整翻译 README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: 按原章节顺序翻译说明文字**

逐节翻译标题、正文、列表、表格说明、警告与故障排查。代码块、环境变量、文件路径、URL、JSON 字段、命令行参数、分数阈值和产品名称保持原样。

- [ ] **Step 2: 统一术语**

全文统一使用以下表达：

```text
baseline → 基线
dry-run → dry-run（试运行）
state → 状态
recheck → 复查
provider → 数据提供方
Issue → GitHub Issue
Trend Opportunity Score → 趋势机会评分（Trend Opportunity Score）
```

- [ ] **Step 3: 保留全部操作风险说明**

确认中文文档仍明确说明：dry-run 会联网并写文件、Google Trends 数据是相对值、独立关键词不可直接横向比较、双源发现全部失败时不改写状态、凭据不得写入仓库。

### Task 3: 验证翻译完整性与项目健康状态

**Files:**
- Verify: `README.md`
- Test: `tests/`

- [ ] **Step 1: 检查 Markdown 围栏与关键标识**

Run:

```bash
python -c 'from pathlib import Path; text=Path("README.md").read_text(); assert text.count("```") % 2 == 0; required=("TRENDS_GEO", "STATE_PATH", "GITHUB_TOKEN", "https://poki.com/en/sitemaps/index.xml", "17 */6 * * *"); assert all(value in text for value in required); print("README structure OK")'
```

Expected: `README structure OK`。

- [ ] **Step 2: 扫描遗留英文标题和长段英文正文**

Run:

```bash
rg -n '^#{1,3} [A-Za-z]|^[A-Za-z][A-Za-z ,.-]{60,}$' README.md
```

Expected: 除必要的项目名或技术标识外，没有英文标题或成段英文正文。

- [ ] **Step 3: 运行完整测试**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: 全部测试通过。

- [ ] **Step 4: 检查文件范围**

Run:

```bash
find README.md docs/superpowers/specs/2026-08-04-readme-chinese-localization-design.md docs/superpowers/plans/2026-08-04-readme-chinese-localization.md -maxdepth 0 -type f -print
```

Expected: 本任务只涉及 README、设计规范和执行计划；不修改源代码或工作流。

当前工作区没有有效 Git 元数据，因此不执行提交步骤。
