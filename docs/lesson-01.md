# 第一课：跟着一篇新闻走完流程

本课先运行和观察结果，再沿着真实代码理解原因。编码可以继续交给 AI；
你的练习是预测改动会影响什么，并用结果验证判断。

## 运行

在项目目录的 PowerShell 中执行：

```powershell
$env:PYTHONPATH="src"
python -m rss_surveillance_agent.demo --db lesson-01.sqlite3
```

样例 101 是财报新闻：假分析器判定相关，类别规则通过，生成 `review` 信号。
样例 102 是食堂新闻：假分析器判定不相关，保存判断但不生成信号。
最后会显示 `articles: 2` 和 `notifications: 1`。

再执行同一条命令。两篇文章都会返回 `duplicate`，数据库数量不变。
第二次执行是新进程，仍记得处理过什么，是因为记录保存在 SQLite 文件中。

## 沿着代码读一次

1. `demo.py` 把 JSON 转为 `FeedEntry`，校验文章 ID、URL、时间等字段。
2. `pipeline.py` 的 `process()` 先查询这个 ID 是否已有处理结果。
3. `analyzers.py` 的 `KeywordAnalyzer` 生成两条摘要、类别和模拟置信度。
4. `rules.py` 用配置判断是否允许该类别产生待复核信号。
5. `storage.py` 在同一个事务中保存原文、分析、规则决定和本地通知记录。

`allow` 只表示当前类别规则允许生成研究信号；`review` 表示交给人复核。
它们都不代表可以买入，也没有任何真实下单动作。

## 你来改变一个行为

现在只允许临床类别，使用另一个数据库以免已有结果被判为重复：

```powershell
python -m rss_surveillance_agent.demo --db lesson-clinical.sqlite3 --allowed-categories clinical
```

先预测：财报新闻仍然被分析为相关，但规则会拒绝它，因此没有信号或通知。
对照结果中的 `analysis.relevant`、`rule_decision` 和 `signal`。
这一步展示了模型判断与业务规则的区别。

再模拟分析超时：

```powershell
python -m rss_surveillance_agent.demo --db lesson-timeout.sqlite3 --simulate-timeout
```

两篇新闻都进入 `review_required`，`analysis` 与 `signal` 是 `null`。
因为没有获得分析，系统无法断言它们相关或不相关，只记录待复核原因。
当前版本把这种结果保留下来，重跑不会自动重试；后续会单独设计重试机制。

## 这一课如何解释给面试官

“我先用假模型固定正常、过滤、失败三类行为。文章处理结果和本地通知在
SQLite 的一个事务内写入，用文章 ID 唯一约束避免重复落库。程序重启后
会查库跳过已处理文章。当前去重限于同一来源的文章 ID；它不解决跨来源
转载去重，也不保证并发模型调用只执行一次。外部通知尚未接入。”

理解这些限制比背这段话更重要。先能解释运行两次为什么不会增加通知，
再进入下一课的失败重试和处理进度。
