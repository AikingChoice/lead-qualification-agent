# 用 VS Code 跟踪第一篇新闻

## 打开项目与启动

用 VS Code 的“文件 → 打开文件夹”打开 `rss-surveillance-agent` 本身。
调试配置位于这个文件夹的 `.vscode/launch.json`。

本机已安装 Microsoft Python 和 Python Debugger 扩展。
如果提示选择解释器，按 `Ctrl+Shift+P`，搜索 `Python: Select Interpreter`，
选择已安装依赖的 Python 3.12：
`C:\Users\32652\AppData\Local\Programs\Python\Python312\python.exe`。
项目默认解释器配置指向本机该位置；换电脑时应重新选择解释器。

打开 `src/rss_surveillance_agent/pipeline.py`，在下面这行左侧、行号旁点击，
出现红点即设置了断点：

```python
if self._store.contains(entry.id):
```

按 `Ctrl+Shift+D` 打开“运行和调试”，选择 `RSS 01 - Fresh run`，按 `F5`。
本项目通过模块入口启动，调试时使用这个配置。
程序输出会出现在底部的 DEBUG CONSOLE（调试控制台），不会通过 PowerShell
集成终端启动，因此也不会触发终端的 PSReadLine 发布者信任提示。

## 暂停时读懂状态

黄色高亮表示下一条将执行的语句，这一行此时还没执行。
鼠标悬停在 `entry` 上，或展开左侧 VARIABLES（变量）里的 `entry`。
第一篇应是 ID 101 的财报新闻。

在 DEBUG CONSOLE（调试控制台）输入这些只读表达式，也能查看对象：

```python
entry.id
entry.title
entry.model_dump()
```

按 `F10` 逐步前进，观察执行顺序。停在 `analysis = ...` 时再按一次 `F10`，
等这行执行完再查看 `analysis`：它应包含 `relevant=True` 和 `category=earnings`。
停在 `rule_decision = evaluate_rules(...)` 时再按一次 `F10`，就能查看规则结果。
暂停在变量赋值之前查询该变量，可能得到 `NameError`，这不是程序故障。

想看函数内部时，在调用那一行按 `F11`。可以进入 `evaluate_rules()`，
查看配置如何决定是否放行；按 `Shift+F11` 执行完当前函数并返回调用处。
`F5` 继续运行到下一个断点；`Shift+F5` 停止。

## 再改变一个条件

先结束调试，再选择 `RSS 02 - Category rejection`。
同一篇财报新闻仍然被分析为相关，但只允许临床类别的规则会拒绝它，
因此 `signal` 保持 `None`。

`RSS 03 - Analysis timeout` 演示异常路径。系统捕获模拟超时并保存待复核结果。
可以在 `except` 内的 `error_code = ...` 一行再打断点。

前三个配置使用 SQLite 内存库，每次启动都从空状态开始。
`RSS 04 - Persistent deduplication` 才使用磁盘文件 `rss-debug.sqlite3`：
完整运行后再次启动，程序会在去重检查后提前返回 `duplicate`。
第一篇处理完以前就停止调试，它可能尚未保存，这时重跑仍会正常处理。

官方说明：https://code.visualstudio.com/docs/python/debugging
