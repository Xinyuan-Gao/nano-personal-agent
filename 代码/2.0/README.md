# Skills 自进化框架 v2.0（代码实现）

本目录是 PRD（[`../../docs/0.2 PRD.md`](../../docs/0.2%20PRD.md)，v0.2）的**可运行实现**：独立运行的本地产品，跑通
「执行 → 反馈 → 候选修改 → 对照评测 → 启用与回退」的完整闭环，并在其上提供 **P1 四层个人记忆**。

- 适用范围：PRD 第 10.2 节的 **75 条需求** = P0 67 条（REQ-SET / SKL / RUN / FBK / LRN / PRP / EVL / VER / SYS）
  + P1 8 条（REQ-MEM-01～08，四层记忆 / 按需召回 / 受控 dream）。
- **不在本版范围**：P2 定时与主动任务、MCP、自动启用、技能整理自动化、多 Agent 编排。
- 技术实现契约见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)（接口签名、数据表、错误码、写入边界、记忆层设计）。
- 运行时**零第三方依赖**：只用 Python 3.11+ 标准库，干净环境可直接启动。
- v2.0 相对 v1.0 的增量：`skillforge/memory/` 记忆层、记忆数据表、任务创建时的记忆冻结与 prompt 注入、
  `memory` CLI / HTTP / Web 入口、记忆指标与数据治理（备份脱敏、按工作区清除、导出）。
  v1.0 的 P0 行为全部保持。

---

## 1. 环境要求

| 项目 | 要求 | 说明 |
| --- | --- | --- |
| Python | 3.11 或更高 | 已实测 3.12 |
| 第三方包 | **无** | SQLite、HTTP、模型调用、记忆检索均用标准库 |
| 模型服务 | 一个 OpenAI 兼容的 `POST {base_url}/chat/completions` | 用户自备；产品不内置账号 |
| Docker | 可选 | 仅「脚本 / 命令执行」需要；缺失时该能力明确不可用，**不会降级为宿主 shell** |
| 系统 | macOS 首批，Linux 可用 | Windows 未验证（PRD A-05） |

## 2. 安装与初始化

```bash
cd 代码/2.0
python3 -m skillforge init                 # 创建数据目录与数据库（默认 ~/.skillforge）
python3 -m skillforge --json doctor        # 可用性检查：模型 / 数据目录 / 容器 / 工具清单
```

也可以先做一次可编辑安装，之后用 `skillforge` 命令：

```bash
python3 -m pip install -e .
skillforge doctor
```

数据目录可用 `--home` 或环境变量 `SKILLFORGE_HOME` 指定，例如：

```bash
export SKILLFORGE_HOME=/tmp/skillforge-demo
python3 -m skillforge init
```

## 3. 快速开始（CLI 全流程）

```bash
# 1) 模型（密钥只写入本机凭据存储，数据库与日志只保留引用）
python3 -m skillforge model set --base-url https://api.example.com/v1 --model gpt-4o-mini --api-key sk-***
python3 -m skillforge model check

# 2) 工作区（可读目录 + 产物目录；默认不授权任意目录）
python3 -m skillforge workspace add --name 文档项目 --root ~/work/docs

# 3) 导入已有 SKILL.md 技能包（原目录只读，包内脚本不会执行）
python3 -m skillforge skill import ~/skills/summary --scope workspace --workspace <workspace_id>
python3 -m skillforge skill show <skill_id>

# 4) 发起任务并等待执行（--session 决定 working memory 的归属）
python3 -m skillforge task run \
  --request "把 references 里的调研笔记整理成一页结构化摘要" \
  --workspace <workspace_id> --session doc-2026w42 \
  --criteria "包含结论段" --criteria "每条结论有来源引用"

# 5) 验收与纠正（执行状态与验收状态分离：completed 不等于 accepted）
python3 -m skillforge task feedback <run_id> --verdict partially_accepted --scope task
python3 -m skillforge task feedback <run_id> \
  --content "结论缺少来源引用，以后同类任务都要给出处" --scope workspace
#    CLI 会顺手把这条纠正作为「可学习事件」入队并立即执行后台学习（--no-learn 可只保存）

# 6) 经验 → 候选 → 冻结评测集 → 配对评测 → 启用
python3 -m skillforge lesson list
python3 -m skillforge propose --skill <skill_id> --lesson <lesson_id>
python3 -m skillforge proposal show <proposal_id>
python3 -m skillforge eval suite-create --skill <skill_id> --name "摘要回归集 v1" --cases cases.json \
  --rubric '{"dimensions":[{"name":"结构","weight":60},{"name":"可追溯","weight":40}],"pass_threshold":70}'
python3 -m skillforge eval start --proposal <proposal_id> --suite <suite_id> --wait
python3 -m skillforge eval report <job_id>
python3 -m skillforge proposal activate <proposal_id> --expected-revision <当前生效 revision>

# 7) 回退（完整包，不只有 SKILL.md）
python3 -m skillforge skill rollback <skill_id> --version <历史版本> --expected-revision <当前生效 revision>
```

所有命令都支持 `--json`，便于脚本消费。

## 4. 个人记忆（v2.0 / P1 四层）

四层结构与边界（PRD 10.2.10）：**记忆存事实，技能存方法，任务状态存本次进展**。

| 层 | 保存内容 | 生命周期 |
| --- | --- | --- |
| working memory | 当前 session 的任务摘要、最近文件与文件短摘要 | 只服务当前 session；文件变化即失效；**不自动升级**为长期事实 |
| daily log | 用户明确记录与模型显式标记的原文、来源、时间、作用域 | 按日期 append-only；可查看 / 导出 / 删除；不直接注入每轮 prompt |
| durable topic | 经显式请求或 dream 整合的稳定事实（用户偏好 / 项目约定 / 关键决策 / 依赖事实） | 可裁决、撤回、删除、过期；冲突保留 `supersedes` 关系 |
| MEMORY.md 索引 | 长期主题的短索引（上限 200 行） | 每轮先注入索引，正文按需读取；可随时从主题表重建 |

```bash
# 记住一条事实（先写日志，默认同时形成长期主题；--log-only 只写日志）
python3 -m skillforge memory remember "以后同类任务每条结论都要标注来源" \
  --scope personal --topic-type user_preferences --title "摘要必须附来源"

# 按需召回（默认最多 3 条；每条可回指来源、作用域与 revision；未命中返回 none）
python3 -m skillforge memory recall "整理调研摘要，结论要附来源" --workspace <workspace_id>

# 查看四层现状：session / 日志 / 主题 / 索引 / dream 门槛
python3 -m skillforge memory status
python3 -m skillforge memory topic --status active
python3 -m skillforge memory log --day 2026-10-12
python3 -m skillforge memory index

# 冲突裁决与遗忘（撤回 / 删除后不再召回，操作写审计）
python3 -m skillforge memory conflicts
python3 -m skillforge memory topic-confirm <topic_id>
python3 -m skillforge memory topic-withdraw <topic_id>
python3 -m skillforge memory topic-delete <topic_id> --confirm

# 受控 dream 整合（手动总是可触发；自动需满足 ≥24h、≥5 个新 session、无并发锁）
python3 -m skillforge memory dream
python3 -m skillforge memory dream-list
python3 -m skillforge memory dream-run <dream_job_id>

# 记忆设置与导出（保留期限、召回条数、开关）
python3 -m skillforge memory settings --recall-limit 3 --retention-days 180
python3 -m skillforge memory expire
python3 -m skillforge memory export --dest /tmp/memory-export
```

实现口径：

- **作用域**：`personal` 跨工作区；`workspace` 仅本工作区；`task` 绑定 session。默认不把 workspace 事实带入其他工作区。
- **来源链**：每条长期主题至少一条来源（log / run / feedback / user_request / dream）；模型推断只能形成 `pending_review`，
  经用户 `topic-confirm` 后才生效（无明确来源的模型猜测不进入长期事实）。
- **召回**：首期用可解释的标签 / 关键词（中文二元切分 + 英文按词）+ 新鲜度排序，不使用 embedding；
  召回记录区分「索引注入」与「正文按需读取」。
- **评测隔离**：评测 run 关闭个人长期记忆（`reason=evaluation_isolation`），保证两组初始环境可比（REQ-EVL-04）。
- **整合边界**：dream 只写记忆表与记忆目录，代码层面不持有 skills / activation 依赖，并自检写入范围；
  整合可在失败或被锁跳过之后重新领取执行。

## 5. 本地 Web 工作台

```bash
python3 -m skillforge serve              # 默认 http://127.0.0.1:8765/
python3 -m skillforge serve --port 8899
```

- PC 本地浏览器为首期支持入口（PRD 10.1.5），页面：任务工作台、技能库与详情、学习建议与候选、评测中心、
  **个人记忆**、设置与数据、后台作业。
- 个人记忆页提供：记忆开关与召回设置、记住一条事实、按需召回（展示匹配依据）、长期主题与待裁决冲突、
  日志与 session、MEMORY.md 索引与重建、手动 dream 与整合历史。
- 仅监听 loopback：非本机 Host 一律拒绝；写操作要求 Origin/Referer 校验 + `X-CSRF-Token` 会话令牌（REQ-SET-04）。
- `serve` 会同时启动后台 worker（最多 2 个执行 worker + 1 个学习 worker），任务、学习与 dream 整合在后台执行。

## 6. 命令清单

| 分组 | 命令 |
| --- | --- |
| 初始化 | `init`、`doctor`、`serve` |
| 模型 | `model set`、`model show`、`model check` |
| 工作区 | `workspace add/list/remove/check` |
| 技能 | `skill import/list/show/versions/archive/restore/export/rollback/manual-trial` |
| 任务 | `task run/list/show/cancel/resume/feedback`（`task run --session` 指定 working memory 的 session） |
| 经验 | `lesson list/ignore/pause/resume` |
| 候选 | `propose`、`proposal list/show/diff/check/reject/activate` |
| 评测 | `eval suite-create/suite-list/suite-show/suite-freeze/start/jobs/report/cancel/resume` |
| 记忆 | `memory status/settings/remember/log/log-delete/log-withdraw/topic/topic-show/topic-confirm/topic-withdraw/topic-delete/index/index-rebuild/recall/recall-history/recall-helpful/conflicts/sessions/session-close/dream/dream-run/dream-list/expire/export` |
| 作业 | `jobs list/cancel/run-pending` |
| 数据 | `data usage/backup/restore/export/clear/preview-clear/retention-show/retention-set/retention-preview/retention-apply` |
| 证据 | `evidence <run_id>`（正文到期后显示「证据不可用」） |
| 指标 | `metrics`（含记忆类三个指标）、`audit` |

## 7. 数据目录与安全边界

```
$SKILLFORGE_HOME/
├─ skillforge.db      元数据（SQLite，WAL；含四层记忆表）
├─ blobs/             内容寻址不可变制品库（sha256，只读）
├─ secrets/           本机凭据（0600，不进备份、不进日志、不进 prompt）
├─ exports/           导出与备份（含 memory export 产物）
├─ memory/            记忆派生文件（MEMORY.md 索引；权威数据仍在数据库）
└─ workspaces/<id>/   每个工作区的 run 产物与评测隔离目录
```

- 包、输入与产物都按 hash 存储；SkillVersion 创建后不可变，启用/回退切换的是完整包指针。
- 路径校验针对**符号链接解析后的真实位置**：目录穿越、绝对路径、symlink 逃逸、越界写入一律拒绝（REQ-SET-02 / REQ-RUN-04）。
- 模型密钥只保存在本机凭据库，运行记录只保留引用；备份与导出不含凭据。
- 备份 `include_body=False` 时记忆正文与摘要一并脱敏；`data clear` 支持按工作区清除记忆，
  清除后主题、日志、索引均不可再召回（REQ-MEM-08 / REQ-SYS-08）。
- 脚本与命令只在非特权容器中执行（`--network none`、输入只读、产物可写、限制 CPU/内存/时间）；容器不可用时该能力明确不可用。

## 8. 验收

```bash
cd 代码/2.0

# 首次启动可用性检查（模型 / 数据目录 / 容器 / 工具）
python3 -m skillforge doctor

# 真实模型 / 真实容器的端到端验证（需要你配置 endpoint 与容器，默认不跑）
python3 scripts/e2e_real.py --base-url https://api.example.com/v1 --model gpt-4o-mini --api-key sk-***
```

## 9. 关键规则（实现口径）

| 主题 | 实现口径 |
| --- | --- |
| 执行 vs 验收 | `execution_status` 与 `outcome` 分离；运行结束默认 `outcome=unknown`，只有客观检查或用户动作才改变（REQ-RUN-08） |
| 技能选择 | 先读轻量索引，默认最多 3 个；只有真正注入上下文的内容才记 `skill_load`，未加载不得作为学习来源（REQ-SKL-05/06） |
| 反馈范围 | “这次/本篇”默认仅本次任务；只有用户显式选择或明确“以后同类任务”才允许 workspace/personal（REQ-FBK-02/03） |
| 归因 | `skill_gap / skill_conflict / model_noncompliance / tool_failure / environment_change / input_gap / unknown`；只有前两类进候选，其余优先建议调整模型/工具/输入/环境（REQ-LRN-03） |
| 硬检查 | 格式可解析、身份不被静默改变、引用完整、路径合法、无明文凭据、脚本依赖声明完整、包大小合规；失败不得进入评测或启用（REQ-PRP-03） |
| 生成隔离 | 候选生成阶段读不到 holdout 夹具与预期答案（REQ-PRP-06） |
| 配对评测 | 5 个样例（≥2 holdout）× 3 次 = 15 配对 / 30 次真实执行；两组仅目标 skill 版本不同；评测 run 关闭学习与个人记忆（REQ-EVL-02/03/04） |
| 结论 | `passed / failed / inconclusive` 由程序按冻结阈值计算，模型不能写 verdict；任一配对缺失不得 passed（REQ-EVL-07/08/12） |
| 启用 | 只接受 `eligible` + 报告 hash 匹配；完整包就绪后在同一事务写 Activation 与 AuditEvent；revision 乐观并发，冲突返回 `baseline_conflict`（REQ-VER-01/02/03） |
| 手工试用 | 生成独立 `manual_trial` 版本，标记「效果未验证」，不计有效改进（REQ-VER-04） |
| 技能注入 | 冻结快照保存被加载技能的**正文**并注入 prompt；只记 `skill_load` 不注入视为未生效 |
| 记忆作用域 | `task` 绑定 session、`workspace` 绑定工作区、`personal` 跨工作区；过滤在 SQL 层，不只靠 UI 隐藏（REQ-MEM-01） |
| 记忆写入 | 长期主题必须有来源；显式请求直接生效，模型推断进入 `pending_review`；非四类主题进入待分类（REQ-MEM-04） |
| 记忆索引 | MEMORY.md 只承载索引与短摘要、上限 200 行；超限截断并保留降级说明；权威数据在主题表，可重建（REQ-MEM-05） |
| 记忆召回 | 可解释（标签/关键词 + 新鲜度）、默认 ≤3 条、命中与未命中都写记录；撤回 / 删除 / 过期不召回（REQ-MEM-06/08） |
| 记忆整合 | 手动可触发；自动需 ≥24h、≥5 个新 session、无并发锁；只写记忆层，失败可重试并留审计（REQ-MEM-07） |
| 重复派发 | 同一 run 用原子执行权抢占保护，评测 worker 与队列 worker 不会重复执行同一任务 |
| 正文保留 | `data retention-set` 设置任务正文保留期限；记忆有独立保留期限（`memory settings --retention-days`）（REQ-SYS-07 / REQ-MEM-08） |
| 预算 | run 30 次调用 / 20 次动作 / 15 分钟 / 100k token；学习 8 次 / 20k / 5 分钟；评测 400 次 / 500k / 60 分钟；超限 `budget_stopped` 且保留产物 |
| 成本 | 缺 usage 或价格时显示未知，不显示 0 元，也不据此判通过（REQ-EVL-11） |

## 10. 已知边界

1. 首期是**单用户本地产品**，无账户、无多租户、无公网监听（PRD 6.4）。
2. P2 能力未实现：定时与主动任务、MCP、自动启用、技能整理自动化；记忆类效果指标需要真实使用数据。
3. 未配置容器时，`run_command` 与含脚本的评测不可用（明确报缺依赖）。
4. 模型服务端权重可能漂移（云侧模型别名）；报告会披露可获得的模型版本，无法获得时注明重现限制。
5. 首次导入的技能会自动成为初始生效版本并标记「效果未验证」，以保证零技能状态可完成首次任务（REQ-SKL-07）。
6. 记忆召回首期使用关键词可解释排序（PRD DEC-09 默认方案），未引入 embedding；中文检索用二元切分覆盖，
   语义改写类查询可能未命中，此时返回 `none` 而不是猜测。
7. dream 整合质量依赖日志质量：模型辅助候选必须引用真实日志来源，否则被丢弃；模型不可用时退回确定性路径。
8. 「有效改进」「试点收益」「记忆召回命中率」等指标需要真实使用与用户反馈，本版只提供带分母与覆盖率的统计口径，
   样本不足时明确 `evidence_insufficient`，不宣称效果。
