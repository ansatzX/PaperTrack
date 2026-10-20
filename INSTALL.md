# 安装与运行

基础要求 Python >=3.10。arXiv 和期刊任务生成、本地导入均不需要 LLM SDK、
Node、Playwright 或项目管理的 Chrome。外部 agent 的浏览器工具由其宿主提供。

## 基础安装

```bash
cd /path/to/PaperTrack
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pip check
python main.py --help
pytest -q
```

已有 Conda 环境可以直接安装，无需再创建 venv。本机验证环境：
`/home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q`。

`uv.lock` 仍是旧项目声明，未与当前 pyproject.toml 同步；不要用于 frozen/locked
安装。当前用 pip 按 pyproject.toml 安装，不承诺所有依赖的精确版本复现。

## arXiv

```bash
python main.py arxiv refresh --category quant-ph --data-dir /path/to/reports
```

这会真实联网查询 arXiv。`run.sh` 保留原有日期桶逻辑和本机部署路径；移植时
核对 Conda、项目与输出路径。不要改动已有环境来满足可选期刊组件。

`run.sh` 不接收参数，只写死一条 `conda run` 调用每日 `refresh` 命令。
循环、日志、锁和失败处理由 Python 包负责。单项任务直接调用包：

```bash
python main.py journal issue --journal jcim --volume 66 --issue 12 --year 2026 --run-agent
python main.py journal refresh --journal jcim  # 检查2026-01-01至今所有正式卷期，只补缺失缓存
python main.py journal latest --journal jcim  # 只打印发现任务
```

输出固定到 `/home/ansatz/data/obsidian/1/papertrack_datas`；
部署路径直接修改脚本中的 Conda 路径、输出参数和工作目录。
脚本强制使用 systemd 用户 scope：内存上限 8 GiB、CPU 200%、禁止 swap、
最多 256 个任务、单条命令最长 24 小时；期刊 Agent 另有默认 660 秒期限，
默认模型为 `gpt-6-luna`。已有 Chrome 不在此资源限制内。
脚本用 Conda 的 arxiv 环境，需在启动前保证 `codex`、`systemd-run`
和 `systemctl` 在 `PATH` 中。`python main.py --help` 查看包命令。
每日执行日志在 `.papertrack_diagnostics/daily.log`，防重入锁阻止同时运行。
单个来源失败后继续其他来源，最终返回非零退出码。脚本显式设置用户总线环境；
仍需已运行的用户总线和可用的授权浏览器连接，无法完成的挑战会明确失败。

## 期刊 agent 任务

已有 Codex CLI 登录和浏览器工具配置时，可以由包直接调用 Agent：

```bash
papertrack journal issue --journal jcim --volume 66 --issue 12 --year 2026 \
  --run-agent --data-dir .work/papertrack-review
```

默认 `gpt-6-luna`，进程超时 660 秒，可用 `--agent-model` 和 `--agent-timeout`
显式调整。只启动一次 Agent 进程，不自动重启、换模型或扩大权限；摘要交互失败可在
同一次任务内用相同固定模块重试一次，同一 DOI 再失败即停止。任务内期限等于进程
上限减去 60 秒，留出返回结果的时间。每次运行在输出目录下建立
独立 `.papertrack-runs/` 子目录，保存任务、事件日志、stderr、最终回复、JSON、HTML
和验收状态。Agent 只采集，包内 Python 验收后才写报告；退出成功不代表采集成功。
超时终止 Agent 进程组，保留局部结果和已有报告。运行目录是新建的非 Git 工作区，
因此使用 `--skip-git-repo-check`，不向 Agent 开放源码仓库作为写入工作区。

Agent 必须通过 `systemd-run --user --scope` 限定资源：内存软上限 6 GiB、硬上限
8 GiB、禁止 swap、CPU 至多两核、至多 256 个任务，运行时限同步进程期限。
缺少 systemd 时直接失败，不回退到无限额启动。这个 cgroup 不包含已经运行的用户
Chrome，不能据此声称 Chrome 也被限制。禁止主动创建/命名标签组及保留任务标签；
成功时适配器关闭任务标签，失败时 Agent 仍需清理并报告无法确认的标签 ID。

JS 模块随 wheel 安装，无需源码仓库或自建浏览器服务。浏览器能力仍由 Codex
宿主提供；只有普通 Codex CLI、没有已连接浏览器工具时，应报告缺少能力并失败。
`latest` / `backfill` 目前仍只生成发现任务，未接入自动执行。
每日 `refresh` 并行运行期刊与 arXiv 两条流程，分别记录日志。期刊流程内部串行：
先读官网归档，按 `--from-date`（默认2026-01-01）至 `--to-date`（默认今天）列出
正式卷期，跟随官网年度/卷目录链接。当天目录缓存可复用，过期或缺失目录才调用
有界 Agent 保存 HTML。Python 检查每一期 capture 和 HTML；完整有效缓存本地
生成报告，只对首次发现且缺少有效缓存的 issue 启动抓取。没有新 issue 就不启动
issue 抓取；已知旧期的缓存缺口标为 `repair_required` 并返回失败，留给显式
`journal refresh` 日期范围修复。已有 Markdown 不代表缓存完整。旧主路径及旧版 `report.md` 仅用于识别已知卷期；未开始采集的计划条目可在下次继续，已启动后中断的期次保留为待显式修复。
目录日期或布局不能解析时明确失败，不让模型临时编写解析器，不伪造完整覆盖。
计划与每期结果保存在 `.papertrack-runs/*-sync-*/plan.json`。

需要手动执行时，省略 `--run-agent`：

```bash
python main.py journal issue --journal jcim --volume 66 --issue 12 --year 2026 \
  --capture-path .work/cache/jcim-66-12.json --data-dir .work/papertrack-review
```

把标准输出交给具备浏览器工具、可以使用用户授权会话的 Codex、Claude Code
或其他 agent。此命令只生成任务，不启动任何 agent，不提供浏览器连接。
agent 负责进入可访问的官方卷期页，然后调用 `browser/collect.mjs` 的固定采集入口。
解析、展开摘要和 JSON 写入由模块完成；agent 不临时编写选择器。访问阻碍无法解决时
保留进度并交接。宿主需提供带 playwright.evaluate/locator 的 browser-client tab，
并允许导入本地 JS 模块；没有这些能力时应明确报错。
Codex 中连接浏览器和导入模块都在同一个 `cua_repl` 内执行；不要把模块放进另一个
`node_repl` 后假设浏览器变量可共享。`browser/cua.mjs` 提供固定的公开 API 适配。

```bash
python main.py journal issue --journal jcim --volume 66 --issue 12 --year 2026 \
  --browser-snapshot .work/cache/jcim-66-12.json --data-dir .work/papertrack-review
```

导入只验收本地数据和生成 Markdown，不向出版社或元数据 API 发请求。
首次真实验收先使用独立输出目录。确认摘要覆盖率、issue 完整性、来源证据和
阅读状态后，再用于自己的阅读仓库。

Zotero 为可选本地服务；需要收藏匹配时启动桌面应用并允许本地 API 访问。
其不可用时保留已有收藏分组；首次报告默认未收藏。Obsidian 不必在采集时运行。

## 可选的原有浏览器工具

`src/papertrack/browser/` 是随包安装的固定采集器，仓库 `browser/` 保留转发入口和
测试；`experiments/journal-workflow/` 是可选的已有队列
原型。采集器在浏览器工具宿主的 Node 运行时内执行，不是普通终端里的独立浏览器
服务，也不负责安装外部 agent 的浏览器能力。基础 Python/arXiv 不依赖它们。
需要使用这些工具时分别阅读目录内 README，Node worker 要求 Node >=22：

```bash
cd experiments/journal-workflow
npm ci --ignore-scripts
npm test
```

worker 仍需宿主提供活跃的浏览器连接，模型只参与有限恢复决策。
该原型报告发布器依赖 POSIX fcntl；不要把它视为已验收 Windows 原生支持。

旧 `run_j.sh`、APS cache runner、HTTP fallback extras 和 Python 浏览器运行时
已从当前源码删除，完整实现保存在 Git 提交 `8804413`。本轮没有修改系统 cron；
若旧部署有相关定时项，应在恢复期刊调度前停用或替换，勿继续调用已删除入口。
已有报告、阅读勾选、旧处理状态、缓存和浏览器配置保留。
