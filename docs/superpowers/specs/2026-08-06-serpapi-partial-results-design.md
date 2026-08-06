# SerpAPI 部分结果容错设计

## 目标

当 SerpAPI 的 `TIMESERIES` 或 `RELATED_QUERIES` 只有一项可用时，保留可用信号并输出安全、可定位的诊断信息；任何报告和日志都不得泄露 API Key。

## 方案选择

采用“两个子结果独立解析”的方案。保持现有 `SerpApiTrendsProvider.research()` 和 `SearchSignals` 接口，不引入新的公共结果类型。相比继续严格失败，该方案能保留部分数据；相比重构整个 Provider 模型，该方案改动更小，能沿用现有报告、评分和序列化流程。

## 数据流

1. `SerpApiTrendsProvider` 仍分别请求 `TIMESERIES` 和 `RELATED_QUERIES`。
2. 两个请求和解析过程相互独立：一个失败不会阻止另一个请求，也不会丢弃另一个已经解析出的结果。
3. 有效 `TIMESERIES` 生成 7、30、90 天趋势值。
4. 有效 `RELATED_QUERIES` 生成 rising queries，并将 `rising_queries_observed` 标记为 `True`；失败时保持 `False`。
5. 子请求失败时，将经过清洗和长度限制的诊断写入 `SearchSignals.errors`。
6. `collect_signals()` 合并 Provider 自身错误与其他外部 Provider 错误，使报告能够显示部分失败原因。

## 错误分类与安全规则

诊断信息包含数据类型、响应顶层键、`search_metadata.status` 和顶层 `error` 摘要。所有内容通过现有错误清洗逻辑处理：控制字符折叠、URL 隐藏、Bearer Token 与常见密钥赋值脱敏、总长度限制为 160 字符。

请求异常、非映射响应、缺少预期字段和字段类型错误均作为对应子请求的错误记录。API Key、完整请求 URL、Authorization Header 和未经清洗的原始响应不得进入错误文本。

## 缓存行为

完整或部分成功的结果继续进入现有 12 小时缓存，避免同一关键词重复消耗额度。两个子请求都失败时不缓存，使后续同一关键词仍有机会重试。

## 测试范围

- TIMESERIES 成功、RELATED_QUERIES 缺失时保留趋势值并报告相关查询错误。
- RELATED_QUERIES 成功、TIMESERIES 缺失时保留 rising queries 并报告时间序列错误。
- 一个网络请求抛出异常时仍执行另一个请求。
- 两个子请求都失败时返回空信号和两条安全错误，不缓存失败结果。
- 顶层 `error`、状态、响应键能够帮助定位，同时 API Key、URL 和 Token 被脱敏。
- `collect_signals()` 保留 Provider 返回的部分数据和错误。
- 既有正常响应、缓存、限速和 Key 轮换行为保持不变。

## 非目标

- 不改变评分阈值或 SEO 动作等级。
- 不增加新的 SerpAPI 请求类型。
- 不在 GitHub Actions 中打印 Secret。
- 不处理历史报告回填；已有游戏由既定复查计划重新获取趋势。
