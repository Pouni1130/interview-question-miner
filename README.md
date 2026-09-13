# mianjing-pipeline · 牛客面经自动整理流水线

> 自动采集牛客网公开面经（主力方向：**Java 后端开发**、**Agent/AI 应用开发**），经 LLM 结构化抽取、跨帖去重，生成按岗位分类的 Markdown 备考文件。
> 每周只需读几份生成的 .md，即可掌握"目标公司目标岗位最近在考什么"。

## 这不是又一个八股文库

本项目整理的是**真实面经帖中被实际问到的问题**——尤其是场景设计题、项目追问和面试官由浅入深的追问链，而非整理好的标准答案题库。

## 功能

- **采集**：牛客公开搜索接口发现面经帖（2026-09 实测有效，含 AJAX 接口 + SSR 详情页双路径）；低频请求（默认 ≥5s）、每日上限、指数退避，绝不绕过登录/验证码
- **粗过滤**：进入 LLM 前，标题 + 正文前 500 字须同时命中"面试词 × 岗位词"双词表（词表可配置），省钱第一道闸
- **LLM 抽取**（OpenAI 兼容协议，DeepSeek/GLM 等均可）：帖子 → 严格 JSON（公司/岗位/轮次/题目/追问链/原文片段），JSON Schema 校验失败自动附带错误重试 2 次；非面经帖自动丢弃；低置信度帖转入人工复核目录；超长帖分块抽取后合并；每日 Token 预算超限自动暂停
- **跨帖去重**：同义归并 + 标准化哈希 + Jaccard 相似度；同题多次出现自动合并计数
- **Markdown 输出**：按岗位分目录的月度汇总（公司 → 轮次分组、追问链嵌套列表、每题附原帖链接）、近 30 天高频题 Top50
- **调度**：`run_daily.bat` + Windows 计划任务，每天无人值守自动运行

## 快速开始

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 填入关键词与 LLM api_key

python -m pipeline collect   # 增量采集
python -m pipeline extract   # 粗过滤 + LLM 抽取
python -m pipeline render    # 生成 Markdown
python -m pipeline run       # 或一键全流程
```

Windows 定时任务（每天 09:00 自动运行）：

```powershell
schtasks /Create /F /SC DAILY /ST 09:00 /TN "MianjingPipeline" /TR "\"<项目路径>\run_daily.bat\" scheduled"
```

## 输出示例

```markdown
## 字节跳动

### 一面

- **设计一个支持10万QPS的短链接服务** `场景设计`
  - 追问:如何解决哈希冲突
  - 追问:如何做高可用
  - 来源:[字节一面面经](https://www.nowcoder.com/discuss/...)
```

## 项目结构

```
pipeline/
├── collect.py   # 采集层:搜索接口 + 详情抓取,限频/退避/增量
├── filter.py    # 粗过滤:双词表闸门
├── extract.py   # LLM 抽取:Schema 校验/重试/分块/预算
├── dedup.py     # 精去重:归一化 + Jaccard
├── store.py     # SQLite:posts 状态机 + questions 题库
└── render.py    # Jinja2 渲染 Markdown
```

## 配置

所有可变项集中在 `config.yaml`：搜索关键词、过滤词表、请求间隔、每日上限、LLM 的 key/model/base_url、每日 Token 预算、输出目录。`config.yaml` 含密钥，已在 `.gitignore` 中排除，请勿提交。

## 合规声明

- 本工具仅采集牛客网**公开可见**内容，低频访问，不绕过任何登录/验证码/付费墙
- 所有输出条目均附原作者的原帖链接；**内容版权归原作者所有，仅供个人学习备考，请勿商用或二次分发**
- 请自行遵守目标网站的服务条款

## Roadmap

- [ ] P2:增量时间窗、合集帖轮次/日期识别改进、pending 人工复核回填
- [ ] P3:GitHub 面经仓库来源、知乎/掘金补充源、高频题时间衰减排序
- [ ] P4:断点恢复强化、失败告警

## 致谢

采集实现与设计参考了以下开源项目（调研结论见项目内文档）：

- [nowcoder-interview-digest](https://github.com/juanjuandog/nowcoder-interview-digest)
- [InterviewRadar](https://github.com/KunChen1110/InterviewRadar)
- [interview-experience](https://github.com/wearzdk/interview-experience)
