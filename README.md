# 面经自动整理流水线

本地采集公开面经，使用 **DeepSeek** 抽取实际面试问题、公司、岗位、轮次和追问链，输出按岗位/月度分类的 Markdown。主要面向 Java 后端与 Agent/AI 应用开发。不生成参考答案。

## 快速开始

需要 Python 3.10+。在项目目录中执行；Windows PowerShell 示例：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml  # 首次安装；已有配置不要覆盖
```

在 `config.yaml` 的 `llm.api_key` 中填写 DeepSeek key，或设置 `DEEPSEEK_API_KEY` 环境变量（优先于配置）。默认保留 OpenAI 兼容接口 `https://api.deepseek.com/v1` 与 `deepseek-chat`；若账户模型名称改变，修改 `llm.model`。

```powershell
# 近 7 个自然日（含今天），牛客为主，CSDN/掘金补充
.\.venv\Scripts\python.exe -m pipeline run --days 7

# 只运行牛客；--limit 仅限制本次抽取的帖子数
.\.venv\Scripts\python.exe -m pipeline run --sources nowcoder --days 7 --limit 3

# 各阶段独立运行
.\.venv\Scripts\python.exe -m pipeline collect --days 7
.\.venv\Scripts\python.exe -m pipeline extract
.\.venv\Scripts\python.exe -m pipeline render
.\.venv\Scripts\python.exe -m pipeline status
```

`python -m pipeline.collect / pipeline.extract / pipeline.render` 也可使用。所有入口共用同一个参数解析器、日志、运行记录和进程锁。自定义配置使用 `--config path/to/config.yaml`，**相对数据/模板路径以配置文件所在目录为基准**。

## 数据来源与访问边界

| 来源 | 发现/读取方式 | 默认配置 |
|---|---|---|
| 牛客 | 公开讨论列表 → 公开 SSR 文章正文 | 启用，优先运行，每日最多 150 帖 |
| CSDN | 公开博客列表或作者 RSS/主页 → 公开正文 | 启用，每日最多 25 帖 |
| 稀土掘金 | 官方文章站点地图最新分片 → 公开文章正文 | 启用，每日最多 25 帖 |
| GitHub | 官方 REST 仓库搜索、Contents、Commits API → 公开 Markdown | 默认关闭，启用后每日最多 10 篇 |

每天所有来源合计最多 200 帖。限额是上限，不保证一定能发现足够的新面经。列表推荐和站点地图可能包含无关文章；进入 LLM 前必须同时命中面试词和岗位词。

2026-09-14 检查发现，牛客网关的 [robots.txt](https://gw-c.nowcoder.com/robots.txt) 禁止 `/api`，[牛客主站](https://www.nowcoder.com/robots.txt)与[掘金](https://juejin.cn/robots.txt)禁止搜索页。因此新版**不再使用旧的牛客搜索 API，也不使用站点搜索页**。

网页采集每次运行读取 robots.txt，未知/不可读规则按暂停处理；逐跳检查重定向，禁止转入登录、验证或支付页面。默认间隔至少 5 秒，按 robots 的更长间隔执行。网络/服务器失败最多重试 3 次；401/403/412/429/451/521 等限制立即停止该来源，其他来源继续。

付费、VIP、订阅、登录才能阅读全文、验证或缺失公开正文的文章整篇跳过。不执行页面验证脚本，不调用隐藏正文接口，不用搜索摘要冒充全文，不更换身份或绕过限制。旧配置中的 `collect.cookie` 已停用；不需要 cookies.json，也不会要求通过刷新登录态获取受限内容。

GitHub 只使用其明确向公共数据开放的 [REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)，不访问网页搜索、私有仓库或提供令牌提高限额。触发 API 额度限制会停止该来源。

来源能否访问还取决于站点当时的服务规则与访问状态。**本次 CSDN 实测遇到 HTTP 521，已按规则跳过；没有声称三个网页来源都已产出真实面经。**

## 配置补充来源

`sources.<source>.discovery_urls` 支持允许访问的 HTML 列表、RSS/Atom、XML sitemap；`seed_urls` 可填明确的公开文章地址。请保持主机名在对应 `hosts` 中。例：

```yaml
sources:
  csdn:
    enabled: true
    discovery_urls: []
    seed_urls:
      - "https://blog.csdn.net/作者/article/details/文章ID"
  juejin:
    enabled: true
    discovery_urls:
      - "https://juejin.cn/sitemap/posts/index1.xml"
    seed_urls: []
  github:
    enabled: false
    repositories: []  # 可填 owner/repo；为空时按 search_query 搜索近期仓库
    search_query: "面经"
```

示例中的作者/ID 是占位符，运行前替换。配置中添加链接不会跳过 robots、付费检查或时间窗。

掘金 Java 标签页在本次请求中没有公开文章链接，因此默认改用官方站点地图。一个分片约 8 MB；下载有 12 MB 上限，只扫描一个分片并截取候选上限，不遍历全站。可用 `seed_urls` 或更有针对性的公开 RSS 降低无关请求。

## 抽取质量、费用与复核

- 模型必须返回完整 JSON Schema；失败时附校验错误重试，首次加两次重试。仍失败标记 `extract_failed`，不会每次自动无限重试。
- 原文作为待分析数据，不执行其中的指令。每题必须带连续原文片段；无法定位的结果进入人工复核。寒暄与自我介绍被过滤。
- `0811` 等不含年份的信息不推测年份；缺失面试日期保留 null。分块传递前文分场标题，不把“面经01”擅自当作“一面”。
- `confidence < 0.6`、不同块的公司归属冲突或证据不足会进入 `output/pending/`。合集可能需要人工拆分，不能只调高置信度就当成正确结果。
- 每次调用前预留输入估算和 `max_tokens` 的预算；用实际 Token 用量结算。超预算前暂停。调用中断/超时且用量未知时保留预留额，避免当作免费重试。下一天自动切换账本。
- `data/usage.json` 保存当日各次调用的帖子 ID、块号、重试号、预留/实际用量；历史账本为 `usage-YYYY-MM-DD.json`。这是配置对应工作目录的预算，不是全账户跨项目预算。
- 分块结果保存在 `data/checkpoints/`。同一内容与抽取配置续跑复用已成功的块；改变模型、Schema 或提示词会重新计算缓存键。

参考 [DeepSeek JSON Output 文档](https://api-docs.deepseek.com/guides/json_mode/)；仍进行本地 Schema 校验，不将 JSON mode 等同于语义正确。

人工复核流程：

1. 在 `output/pending/<fingerprint>.json` 中对照 `post_url` 与本地 raw 修改 `result`。保留 `post_fingerprint`；修正题目、原文证据、日期、归属和 confidence。非面经设 `is_interview_post: false`。
2. 提交复核：

```powershell
python -m pipeline review --review-file "output/pending/<fingerprint>.json"
```

程序再次校验，再事务入库并渲染，生成 `.reviewed.json` 审计副本。已审核帖子不会再次累计计数；pending 目录保留历史文件，当前待处理状态以 `python -m pipeline status` 和数据库为准。

## 去重、日期与输出

原帖唯一指纹为 URL + 发布时间。正文保存在 `raw/{source}/{采集日期}/{post_id}.json`，同一记录永不覆盖；同一日的不同发布时间版本使用指纹后缀。

`questions` 存规范题目，`occurrences` 存每个来源中的公司、轮次、日期和追问。频次按不同原帖 URL 计数，同帖不同轮次、重新抽取或同 URL 版本不会虚增。完整帖子入库、出现记录重建和最终状态在同一事务内完成。

输出：

```text
output/
├── Java后端/YYYY-MM.md
├── Agent开发/YYYY-MM.md
├── 高频题/近30天高频题Top50.md
└── pending/<fingerprint>.json
```

月度归档使用有明确依据的面试日期，否则使用发布日期并标明“面试日期未知”。历史帖子不会因为今天抽取就混入本月。高频榜仅统计近 30 天至少 3 个不同原帖的题目，按每个来源的 `2^(-距今天数/14)` 求和排序。无符合条件的题目就输出空榜，不以一次题填满 Top50。每题保留原帖链接。

模板独立位于 `templates/`，内容通过 Jinja2 渲染。输出使用原子替换；原始文章不可变。

## 旧版本升级、恢复与运行状态

第一次打开旧数据库时，会创建 `data/interviews.pre-v2-时间.db` 备份，保留 `legacy_posts/legacy_questions`，将旧成功抽取/待复核帖子排队重新抽取。旧频率、猜测日期不直接迁移为可信数据。本次升级前的输出另存于 `data/backups/output-pre-v2-时间/`。

```powershell
# 恢复“raw 已完整写入、数据库尚未提交”时中断的帖子
python -m pipeline recover

# 只重试明确失败的帖子（继续复用已成功的分块），然后更新输出
python -m pipeline retry-failed
python -m pipeline render

# 因预算暂停的帖子直接继续 extract；不要删除 usage.json 绕过预算
python -m pipeline extract

# 修改排除词/日期规则后，免费重新检查已保存结果并更新输出
python -m pipeline revalidate
```

OS 进程锁避免同一个数据库的定时任务重叠。进程结束后锁自动释放，磁盘上的 lock 文件可保留。`runs` 表记录开始、完成、partial、failed 和 interrupted；日志在控制台和 `logs/pipeline.log`，自动轮转。

退出码：0 为本次命令完成；1 为失败；2 为部分完成（例如来源访问受限、达到请求预算或仍有待抽取帖子）。待人工复核会打印提示，不会阻止其他帖子处理。退出码 0 不代表连续三天稳定性或人工准确率已经验收。

## Windows 定时与 cron 示例

`run_daily.bat` 自动创建日志目录，优先使用 `.venv\Scripts\python.exe`，保留 Python 退出码。手动双击会暂停，计划任务使用 `scheduled` 参数。

可在 Windows“任务计划程序”创建每天 09:00 的任务，操作为：

- 程序：`C:\Windows\System32\cmd.exe`
- 参数：`/d /c ""D:\你的项目目录\run_daily.bat" scheduled"`
- 起始于：`D:\你的项目目录`
- 如果任务已运行，选择“不启动新实例”。

也可在 PowerShell 中创建（请替换路径；本项目不会自行注册任务）：

```powershell
$projectPath = (Get-Location).Path
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" -Argument ('/d /c ""{0}\run_daily.bat" scheduled"' -f $projectPath) -WorkingDirectory $projectPath
$trigger = New-ScheduledTaskTrigger -Daily -At '09:00'
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'MianjingPipeline' -Action $action -Trigger $trigger -Settings $settings
```

Linux/macOS cron 示例（先手动验证解释器和配置路径）：

```cron
0 9 * * * cd /absolute/project && /absolute/project/.venv/bin/python -m pipeline run --days 7
```

## 测试与验收

```powershell
python -m pip install -r requirements-dev.txt
python scripts/run_tests.py
```

测试只使用临时数据库与模拟 HTTP/LLM，不读取真实 key，不调用外部服务。覆盖 CLI、迁移备份、事务回滚、不同来源计数、30 天与月度边界、预算与断点、访问限制、付费检测和复核。

实测结果与未完成的长期验收见 `docs/完成情况与验收-2026-09-14.md`。连续三天运行需要自然时间，不能由单次离线测试替代；使用 `runs` 和日志核对。

## 个人学习与署名

内容版权归原作者所有，仅供个人学习，不作商用或未经许可的再次分发。所有输出带来源链接和声明。请遵守各站服务条款；robots 允许不等于任何用途都得到授权。

`config.yaml`、`.env`、cookies、raw、data、logs、output 和本地验证目录均被 Git 忽略。项目没有内置真实凭据。采集/解析设计参考 InterviewRadar、nowcoder-interview-digest，Markdown 输出参考 interview-experience；新版已替换受限制的旧采集路径。
