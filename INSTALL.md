# 安装与运行

本文覆盖基础 PaperTrack、可选的期刊浏览器工作流、模型接入及旧脚本。
**只运行 arXiv 时，完成第 1、2 节即可；不需要安装 Node、Chrome 或配置 LLM。**

当前完整工作流以 Linux 为验证环境。Python 要求 >=3.10，已验证的环境为
Python 3.10.16；新期刊模块要求 Node >=22。旧 supervisor 依赖 Linux pidfd、
`/proc`，新报告发布器使用 POSIX `fcntl`，因此不能把整个项目视为已支持
Windows 原生运行。无需 GPU、模型权重、Docker 或数据库服务。

## 1. 安装基础 Python 项目

先取得包含 `pyproject.toml` 和 `src/papertrack/` 的完整项目源码。
以下命令从项目根目录执行；示例中的路径需要替换成实际路径。

### 新建隔离环境

先通过系统包管理器或已有 Python 管理工具准备 Python。下面使用已验证的
Python 3.10；机器上需要存在 `python3.10` 命令。

```bash
cd /path/to/PaperTrack
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m pip check
python -c 'import papertrack; print(papertrack.__file__)'
python main.py --help
```

`-e .` 将当前源码安装为可编辑项目，也注册 `papertrack` CLI。移动项目目录后
需重新安装。运行 `main.py` 仍应处于项目根目录；也可使用已安装的 `papertrack` 命令。

若已有 Conda 环境，可直接在该环境内安装，**无需同时创建 venv**：

```bash
conda activate arxiv
python -m pip install -e .
python -m pip check
```

本机已验证的解释器为
`/home/ansatz/soft/miniconda3/envs/arxiv/bin/python`；这是部署示例，不是其他机器
必须使用的路径。不要为了新增期刊功能升级或覆盖已稳定运行的 arXiv 环境。

基础包由 `pyproject.toml` 安装：pyzotero、bs4/BeautifulSoup、lxml、feedparser、
Jinja2、curl_cffi、requests；Python <3.11 还安装 tomli。
若所用平台没有依赖包的二进制 wheel，从源码构建可能需要额外编译工具。

**当前 `uv.lock` 与 pyproject.toml 不一致**：它仍记录旧项目名和 Python >=3.6。
在重新生成并验证之前，不要使用这个旧锁文件执行 frozen/locked 安装。
上述 pip 命令按当前声明安装，但不是全部 Python 包的精确版本复现方案。

## 2. 基础配置与运行

- 类别和期刊配置：`src/papertrack/categories.toml`。
- 输出目录：显式传 `--data-dir`。现有 CLI 默认目录是作者机器上的绝对路径。
- 网络：arXiv 需访问 arxiv.org；期刊需访问相应出版社和所用元数据服务。
- 输出、缓存、诊断目录必须可写；不要把含阅读勾选的 Markdown 当成临时文件删除。

以下是**真实联网抓取**示例，不是离线安装检查；根据需要选择运行：

```bash
# 只处理一个历史日期，输出到独立测试目录。
python main.py arxiv backfill --category quant-ph \
  --from-date 2026-09-01 --to-date 2026-09-01 \
  --data-dir /tmp/papertrack-check

# 普通期刊入口：HTTP 抓取和已实现的第三方补缺。
python main.py journal issue --journal jcim --volume 66 --issue 12 \
  --year 2026 --data-dir /tmp/papertrack-check
```

`chem-ph`、`quant-ph`、`cs.AI`、`cs.LG` 等按配置使用。arXiv 采用提交日期桶，
不要用“最新 feed”替代这个流程。出版社能通过浏览器访问，不保证普通 HTTP
入口也能读取；遇到该情况，可选择第 4 节的已连接浏览器流程。

### Zotero 与 Obsidian（应用均可选）

收藏匹配默认使用 Zotero 桌面程序的本地 API：`http://localhost:23119/api`。
需要匹配时，启动 Zotero 并在其设置中允许本地 API 访问。无需提供 Zotero 云端
密钥。不可用时仍可生成报告，只是没有可用的收藏匹配结果。

Obsidian 用来阅读生成的 Markdown，不需要在抓取时运行。直接指定其仓库内目录
作为 `--data-dir` 即可；首次验证建议先使用 `/tmp` 或其他独立目录。

## 3. Python 可选功能与测试

在已激活的 Python 环境、项目根目录执行，按需选择：

```bash
python -m pip install -e '.[dev]'             # pytest 和 Python Playwright
python -m pip install -e '.[browser]'         # 仅增加 Python Playwright
python -m pip install -e '.[http-fallbacks]'   # 额外 HTTP 后端
```

HTTP extras 包含 cloudscraper、primp、tls-client、pyreqwest-impersonate，基础
arXiv 并不要求这些包全装。Python Playwright 用于旧浏览器路径，不等于新工作流
所需的 Chrome 扩展宿主。旧脚本使用已安装的 Chrome；不要把下载 Playwright
Chromium 当成所有入口都必须执行的步骤。

```bash
python -m pytest -q
```

默认回归测试使用模拟网络；真正的出版社访问、浏览器连接和模型 API 必须另行
验证。旧 `test.ipynb` 含历史 `ArXiv_Tools` 导入，不作为当前安装验收入口。

## 4. 安装独立的期刊工作流

这部分位于 `journal-workflow/`，不改变基础 Python 入口或依赖。它包含 Crawlee
队列与重试、断点保存、报告发布，以及可选 agent 恢复判断。

### 4.1 Node 与 npm

先准备 Node >=22 及与之兼容的 npm，再检查当前 PATH 实际选择的版本：

```bash
node --version
npm --version
cd journal-workflow
npm ci --ignore-scripts
npm test
cd ..
```

依赖由该目录自己的 `package-lock.json` 锁定：`@crawlee/basic`、`@crawlee/core`、
`proper-lockfile`、官方 `openai` SDK。不要在项目根目录执行 npm 安装。

**本机 PATH 注意事项**：普通 `node` 当前是 20，而 `/usr/bin/node` 是 22。
npm 的可执行脚本也会从 PATH 找 node。本机可这样确保两者一致：

```bash
cd journal-workflow
PATH=/usr/bin:/bin /usr/bin/npm ci --ignore-scripts
PATH=/usr/bin:/bin /usr/bin/npm test
cd ..
```

其他机器应使用其实际的 Node 22+ 安装位置，不必照抄 `/usr/bin`。

### 4.2 浏览器宿主是额外前提

需要准备：

1. 能正常打开目标出版社的 Chrome 和可用图形会话。
2. 兼容的 agent 浏览器工具宿主、Chrome 扩展及本机通信组件。
3. 通过该宿主的浏览器技能建立连接，获得 `browser` 和受支持的 tab API。

当前验证使用 agent 环境提供的 browser-client。**它不是本仓库通过 npm ci
自动安装的独立浏览器服务。** 没有这个宿主时，Python 基础功能仍可用，但不能
照搬下面的工具内 JS 到普通 Node 终端来控制 Chrome。

现有 driver 与 worker 通过同一文件系统的任务目录交换消息。当前两者应在能
访问同一目录的环境运行；跨机器部署尚未实现。浏览器登录、重新连接由宿主负责。
不要复制浏览器凭据，也不要把浏览器连接对象写入任务 JSON。

### 4.3 启动 worker 和连接 driver

从项目根目录，在终端启动 worker；将 Python 路径替换成第 1 节的解释器：

```bash
node journal-workflow/cli.mjs \
  --job journal-workflow/example-job.json \
  --dir /tmp/papertrack-jcim-job \
  --output /tmp/papertrack-workflow-output \
  --python /absolute/path/to/python \
  --agent off
```

示例任务是 JCIM 66/12。其他任务应修改 journal、正式 issue URL、volume、issue、
year；当前新入口支持 jcim、jctc、jcp、prl。`--dir` 是该任务的持久状态目录，
`--output` 是报告根目录，两者可以不同。

然后在**已连接的浏览器工具运行环境**中执行以下 JS。`browser` 必须由宿主先建立；
导入路径和任务目录必须与实际安装一致：

```js
var workflow = await import('/absolute/path/to/PaperTrack/journal-workflow/browser-driver.mjs');
var issueTab = await browser.tabs.new();
var driver = workflow.createBrowserDriver({tab: issueTab});
// APS 还需单独的摘要页标签：
// var abstractTab = await browser.tabs.new();
// var driver = workflow.createBrowserDriver({tab: issueTab, abstractTab});
await workflow.serveBrowser('/tmp/papertrack-jcim-job', driver, {durationMs: 25000});
```

宿主需继续调用 `serveBrowser`，直到 `status.json` 为 `complete` 或
`needs_attention`。调用分段是为了适配工具时限；队列推进由 worker 决定。
worker 不是无人值守 Chrome 启动器；若 driver 没有及时连接，命令会超时并
返回可见失败。完成后由宿主释放任务拥有的标签页。

重连后新建 driver，再用同一任务目录重跑 worker，可继续已保存的文章。
不要同时运行两个 driver 或两个 worker 写同一任务。已完成任务会复用结果；
若要重新抓官网变化，使用新任务目录。不要让旧、新报告写入器同时修改同一期输出。

新 renderer 使用官方抓取结果离线生成报告。若要走原有第三方补缺管线，可另行
使用已有快照（此命令不启动 Chrome，补缺可能联网）：

```bash
python main.py journal issue --journal jcim --volume 66 --issue 12 --year 2026 \
  --browser-snapshot /tmp/papertrack-jcim-job/capture.json \
  --data-dir /tmp/papertrack-enriched
```

## 5. 可选 LLM 接入

模型 key 由运行 worker 的进程环境提供。SDK 不会自动加载本项目的 `.env`；
通过自己的环境管理方式注入，勿写入任务 JSON、源码或日志。

| 选择 | 环境变量 | worker 参数 |
|---|---|---|
| 不用模型 | 无 | `--agent off` |
| DeepSeek | `DEEPSEEK_API_KEY` | `--agent deepseek --model deepseek-flash` |
| OpenAI | `OPENAI_API_KEY` | `--agent openai --model gpt-5.6-luna` |

把第 4 节 worker 命令的 `--agent off` 替换成相应参数即可。正常采集不调用模型；
只有异常处理才需要 key。缺 key 不破坏正常流程，恢复时会返回 model_unavailable。
DeepSeek 已实测真实 SDK 调用；OpenAI 路径已做契约测试，仍需用目标账号验证访问。

默认 agent 只看状态和相关 DOM。自动截图升级尚未接入默认 runner，不能把模型
具有视觉能力当成该流程已经会自动使用截图。

可选第三方元数据变量为 `OPENALEX_API_KEY`、`SEMANTIC_SCHOLAR_API_KEY`。
它们不是运行 arXiv 的前提；服务拒绝匿名访问时会留下缺口，不保证无 key 全部可用。

## 6. 旧启动脚本和定时运行

`run.sh` 是现有 arXiv 日期桶流程；`run_j.sh`、`run_aps_issue_cache.sh` 属于旧期刊
执行路径，不是第 4 节新 worker 的别名。

这些脚本包含项目、Conda、输出目录等本机绝对路径，并可能 source 用户的
`.bashrc`。迁移时先核对这些路径、环境名、类别及回填范围，再手动验收；不要直接
把未经检查的脚本放进 cron。新工作流目前不随基础安装自动启用定时任务。

旧 `run_j.sh` 还要求 Linux、Bash（支持 wait -n -p）、系统 Python、pidfd、setsid、
Google Chrome、xvfb-run、Xvfb、xauth 和 Conda arxiv 环境。可做只读预检：

```bash
command -v google-chrome-stable xvfb-run Xvfb xauth setsid
/usr/bin/python3 scripts/run_j_worker_supervisor.py check-pidfd
```

所需系统包通过所在发行版的包管理器安装；不要把这些 Linux 专用组件作为
所有用户安装 arXiv 功能的强制前置条件。

## 7. 验收与保留状态

安装后按启用范围验证：基础 `pip check`、CLI help、Python 测试；新模块再运行
npm test；最后用独立输出目录验证真实浏览器和所选模型。测试通过不等于出版社
授予访问权限。

模型真实调用测试是显式操作，会产生 API 请求：

```bash
cd journal-workflow
node test/live-model.mjs deepseek /tmp/papertrack-model-check.json
```

保留报告及阅读勾选、原 `.papertrack_state.json`、官方快照、新任务目录与备份。
不要在两个版本间共享正在写入的状态目录。

详细边界和已完成实测见 [journal-workflow/README.md](journal-workflow/README.md)、
[journal-workflow/VALIDATION.md](journal-workflow/VALIDATION.md) 和
[browser/README.md](browser/README.md)。
