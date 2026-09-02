# push2xteink 设计文档

日期：2026-08-31
状态：待实现

## 1. 目标

在一台常开的 Linux 服务器上，用单个 Docker 容器运行一个服务：

- 配置若干 RSS 源和「推送任务」
- 定时执行任务：抓取各源的新文章 → 可选抓全文 → 可选 AI 总结 → 生成 EPUB/TXT → 上传到 xteink，文件自动出现在绑定的阅读器上
- 提供一个轻量 Web 管理界面维护配置、查看执行历史、手动触发

非目标：多用户、内容存档、阅读器功能、统计图表、RSS 在线阅读。

## 2. 技术选型

- Python 3.12
- FastAPI —— Web API + 服务端渲染页面
- HTMX（配极简 Alpine.js）—— 前端交互，不引入前端构建链
- APScheduler —— cron 调度，支持热重载
- feedparser —— RSS/Atom 解析
- trafilatura —— 正文提取
- ebooklib —— EPUB 生成
- httpx —— 所有 HTTP 请求（支持代理）
- pydantic v2 —— 配置与数据模型校验
- ruamel.yaml —— 读写 config.yaml 并保留注释
- SQLite（标准库 sqlite3）—— 状态存储
- 测试：pytest、respx（HTTP mock）

部署为单容器单进程，Web 与调度器在同一进程内。`./data` 卷挂载，内含 `config.yaml` 和 `state.db`。

## 3. 架构总览

```
                    ┌─────────────────────────────────┐
  config.yaml ────► │  push2xteink (单进程 Docker)     │
                    │                                 │
                    │  ├── Web 管理界面 (FastAPI+HTMX) │  ◄── 浏览器
                    │  ├── 调度器 (APScheduler)        │
                    │  └── 任务执行流水线              │
                    │      fetch → dedup → extract →   │
                    │      summarize → build → upload  │
                    │                                 │
  state.db ◄──────► │  (SQLite: seen_items / runs)     │
                    └─────────────────────────────────┘
                             │
                             ▼  四步上传
                    签名 → 阿里云 OSS → callback → device/tasks
                             │
                             ▼
                    绑定的阅读器（阅星曈 X4）
```

模块边界（`src/push2xteink/`）：

| 模块 | 职责 | 依赖 |
|---|---|---|
| `config.py` | 加载 / 校验 / 写回 `config.yaml` | models, ruamel.yaml |
| `models.py` | 所有配置段的 pydantic 模型 | pydantic |
| `state.py` | SQLite 读写：`seen_items`、`runs` | sqlite3 |
| `feeds.py` | 拉取 RSS、按 `seen_items` 去重、写入新条目 | feedparser, httpx, state |
| `extract.py` | 抓文章链接、正文提取、失败回退 | trafilatura, httpx |
| `summarize.py` | OpenAI 兼容客户端，primary/fallback 切换 | httpx |
| `builders/epub.py` `builders/txt.py` | 由条目列表生成文件 | ebooklib |
| `xteink.py` | `XteinkClient`：登录 + 三步上传 | httpx, state（token 缓存） |
| `pipeline.py` | 编排单个任务的执行（步骤 1→7） | 上述全部 |
| `scheduler.py` | 从 config 装载 cron 任务，热重载 | APScheduler, pipeline |
| `web/app.py` | REST API + HTMX 页面 | config, state, scheduler, pipeline |
| `__main__.py` | 启动 web + scheduler 单进程 | web, scheduler |

## 4. 配置格式（config.yaml）

Web 界面与手工编辑读写同一个文件。所有改动写回后热更新调度器，无需重启容器。

```yaml
xteink:
  api_base: https://api-prod.xteink.cn
  username: "<手机号>"
  password: "<密码>"

proxy:
  url: http://127.0.0.1:7890        # 或 socks5://host:port；空则无代理

ai:
  use_proxy: false                  # AI 请求是否走 proxy.url
  primary:
    base_url: https://api.example.com/v1
    api_key: "<key>"
    model: gpt-4o-mini
  fallback:                         # 可选；primary 失败时启用
    base_url: https://api.backup.com/v1
    api_key: "<key>"
    model: claude-3-5-haiku
  prompt: |                         # 可选，有内置默认值
    用中文简洁总结以下文章要点，3-5 条。
  timeout_seconds: 60
  max_retries: 2
  qps: 1                            # AI 调用限流

fetch:
  timeout_seconds: 20
  concurrency: 5

feeds:
  - id: hn
    url: https://news.ycombinator.com/rss
    full_text: true                 # 默认 true；失败回退 RSS 内容
    use_proxy: true                  # 默认 false；该源 RSS 抓取 + 全文抓取走代理
  - id: ruanyifeng
    url: http://www.ruanyifeng.com/blog/atom.xml

tasks:
  - id: morning-brief
    name: 早报
    feeds: [hn, ruanyifeng]
    schedule: "0 7 * * *"           # 标准 5 段 cron
    summarize: true                  # 默认 false
    format: epub                     # epub | txt；默认 epub
    enabled: true
    first_run_lookback_hours: 48     # 仅任务从未成功执行过时生效
    max_age_hours: 48                # 可选；每次执行都只保留 published_at 在此窗口内的文章（无日期的保留）
    max_items: 3                     # 可选；过滤后按发布时间取最新 N 篇（跨该任务所有源合计）
```

`max_age_hours` / `max_items` 用于 backlog 很长的源（如每日汇总类 RSS）：不设则不限制，
行为同旧版。`max_age_hours` 是每次执行都生效的时间过滤（不同于只在首次生效的
`first_run_lookback_hours`）；`max_items` 是过滤之后的硬上限。被 `max_items` 截掉的文章
已记入 `seen_items` 但未推送，仍可在 `first_run_lookback_hours` 窗口内于后续执行补推
（与上传失败的重试路径一致），超期则放弃。

校验规则：

- `tasks[].feeds` 中每个 id 必须在 `feeds` 中存在
- `tasks[].id` / `feeds[].id` 唯一
- `schedule` 必须是合法 cron
- `summarize: true` 时 `ai.primary` 必须完整
- `format` ∈ {epub, txt}
- `max_age_hours` / `max_items` 若设置必须 > 0

## 5. 状态存储（state.db，SQLite）

用户不直接接触。

```sql
CREATE TABLE seen_items (
  feed_id     TEXT NOT NULL,
  item_guid   TEXT NOT NULL,        -- RSS entry id/guid，缺失时回退 link
  first_seen_at TEXT NOT NULL,      -- ISO8601 UTC
  pushed_at   TEXT,                 -- 上传成功后填入；NULL = 尚未推送
  PRIMARY KEY (feed_id, item_guid)
);

CREATE TABLE runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT NOT NULL,        -- running | success | skipped | failed
  item_count  INTEGER,
  file_name   TEXT,
  message     TEXT                  -- 错误或告警详情（多行）
);

CREATE TABLE kv (
  key   TEXT PRIMARY KEY,           -- 例：xteink_access_token
  value TEXT,
  updated_at TEXT
);
```

token 缓存放 `kv` 表（`xteink_access_token` + `xteink_token_obtained_at`）。

## 6. 任务执行流水线（pipeline.py）

调度器或 Web「立即执行」触发某个 task，走同一段代码：

```
0. 在 runs 插入一行 status=running

1. 加载源
   对 task.feeds 每个 feed：feedparser 拉取（use_proxy 则走代理）
   拉取失败 → 记入本次 run 的告警，跳过该源，继续其它源

2. 去重
   对每个 entry 取 guid（id/guid，缺失回退 link）
   查 seen_items：已存在 → 跳过
   新 entry：
     若该 task 在 runs 中没有任何 status=success 记录（首次）→
       仅保留发布时间在 now - first_run_lookback_hours 之内的
     若设了 task.max_age_hours → 丢弃 published_at 早于 now - max_age_hours 的
       （无 published_at 的保留）
     写入 seen_items（pushed_at = NULL）
   跨该任务所有源汇总后，若设了 task.max_items 且条数超过它 →
     按 published_at 降序（无日期排最后）只保留最新的 max_items 条
   得到本次待推送条目列表 items；为空 → 跳到步骤 7，status=skipped

3. 取正文
   对每个 item：
     feed.full_text=true → 用 httpx 抓 item.link（按 feed.use_proxy）
       → trafilatura 提取正文
       → 失败 / 超时 / 正文过短(<200 字) → 回退用 RSS 的 content/summary
     feed.full_text=false → 直接用 RSS content/summary
   条目间用并发池（fetch.concurrency）

4. AI 总结（仅 task.summarize=true）
   对每个 item 调 ai.primary（遵守 ai.qps 限流、timeout、max_retries）
     异常/超时耗尽重试 → 若配置了 ai.fallback，用 fallback 再试一轮
     仍失败 → 该条跳过总结，记 run 告警
   章节正文结构：
     [AI 摘要段落] + <hr/> + [正文 HTML]
   summarize=false → 章节只有正文

5. 生成文件
   标题：f"{task.name}_{YYYYMMDD}"（同日同任务重复执行追加 _{HHMMSS}）
   format=epub → ebooklib：
     书名 = 标题；每个 item 一章；生成目录(NCX/nav)
     章节头部含文章标题、来源、原文链接、发布时间
   format=txt → 纯文本：每条 "# 标题\n来源 · 链接 · 时间\n\n[摘要]\n---\n[正文纯文本]"，条目间空行 + 分隔线
   EPUB 成品 < 256 字节 → 视为异常，status=failed，不上传

6. 上传
   XteinkClient.push_file(path, filename)
   成功 → 把本次 items 对应 seen_items 行的 pushed_at 置为 now
   失败（XteinkUploadError）→ 不更新 pushed_at；status=failed；message 记错误
     下次执行时这些条目仍无 pushed_at，但 guid 已在 seen_items —— 见下方说明

7. 收尾
   更新 runs 行：finished_at、status、item_count、file_name、message

去重与重试的一致性：
- 步骤 2 写入 seen_items 时 pushed_at=NULL。
- 判定「新条目」的依据是 guid 是否存在于 seen_items —— 因此上传失败后，
  下次执行同一任务时这些条目不会被重新当作「新」。
- 解决：步骤 2 的「新条目」判定改为 `guid 不存在 OR (存在且 pushed_at IS NULL
  且 first_seen_at 在最近 N 小时内)`，N = first_run_lookback_hours。
  即未成功推送的条目在窗口期内会被重试，超期则放弃，避免永久卡住或无限堆积。
```

失败隔离原则：

- 单个源拉取失败不影响其它源
- 单条正文抓取失败 → 回退，不影响该条入库
- 单条 AI 总结失败 → 跳过总结，不影响成文
- 上传失败 → 整个 run failed，条目按上面规则在窗口内重试

## 7. xteink 上传器（xteink.py）

对外接口：`XteinkClient.push_file(path: Path, filename: str) -> str`（返回 record_id）。

已通过抓包确认的协议（base = `https://api-prod.xteink.cn`）：

### 7.1 登录

```
POST /auth/login
body(JSON): {"username": "...", "password": "..."}
resp: {"access_token": "...", "refresh_token": "...", "expires_in": <秒>, ...}
```

- access_token 有效期约 29 天。不使用 refresh 接口；token 过期或不存在时用账号密码重新登录。
- token 与获取时间缓存在 `state.kv`。判定过期：`now - obtained_at > expires_in - 1天` 的安全余量。
- 后续所有 api-prod 请求头：`Authorization: Bearer <access_token>`。
- 任一 api-prod 请求返回 401 → 清除缓存 token，重新登录一次并重试该请求；再失败则抛错。

### 7.2 上传（四步：签名 → OSS → 回调 → 推送到设备）

```
步骤 A — 申请签名
POST /api/v1/upload/signature
body(JSON): {
  "filename": "<标题>_<YYYYMMDD>.epub",
  "content_type": "application/epub+zip",   # txt → "text/plain"
  "file_md5": "<hex md5>",
  "file_size": <bytes>,
  "prefix": "uploads/book"
}
resp: {
  "success": true,
  "host": "https://domestic-static-file.oss-cn-hangzhou.aliyuncs.com",
  "key": "uploads/book/.../<filename>",
  "policy": "<base64>",
  "signature": "<base64>",
  "access_key_id": "LTAI...",
  "download_url": "...",
  "instant_upload": <bool>
}
# instant_upload=true 时 OSS 已有同 md5 文件，可跳过步骤 B。

步骤 B — 上传到 OSS
POST {host}
content-type: multipart/form-data
fields:
  key           = <resp.key>
  policy        = <resp.policy>
  OSSAccessKeyId= <resp.access_key_id>
  signature     = <resp.signature>
  Content-Type  = <content_type>
  file          = <文件二进制>
期望响应: 204 No Content
（此步骤不带 Authorization 头，不走代理）

步骤 C — 回调确认
POST /api/v1/upload/callback
body(JSON): {
  "oss_key": <resp.key>,
  "filename": <filename>,
  "file_size": <bytes>,
  "file_md5": "<hex md5>",
  "content_type": "application/epub+zip"
}
resp: {"success": true, "record_id": "...", "download_url": "...", "size_mb": ...}

步骤 D — 推送到设备（**必需**）
GET /api/v1/device/binding
  → resp.data[]，取 selected=true 的一项（否则取第一项）的 device_id
POST /api/v1/device/tasks
body(JSON): {
  "device_id": <上面的 device_id>,
  "file_url": <signature 的 download_url，或 {host}/{key} 拼接>,
  "save_path": "/Pushed Books/<filename>",
  "points_source": "playmethod",
  "func_code": "h5-file-upload"
}
resp 201: {"success": true, "task": {"task_id": "...", "status": "processing",
           "task_type": <服务端按文件类型自动选：txt→txt_encoding_fix，epub→epub_xtg_push>, ...}}
```

**步骤 A–C 只是把文件暂存到 OSS，不做步骤 D 的话文件不会进账号、也不会同步到阅读器**
（早期抓包漏了这一步，导致「上传成功但云端/设备都收不到」）。任务完成后会得到
`book_content_id`，再由手机 App 经蓝牙同步到 X4。

设备信息（`GET /api/v1/device/binding`）：
绑定设备 `阅星曈 X4`，480×800，binding id `836d59b7-9263-44bd-aba2-04fd786d2eb1`，
device_id `10285164_7C_E8_B1_9C_F0_6C`。
注意：`GET /api/v1/device/tasks` 列表接口在账号存在 `txt_encoding_fix` 类型任务时会 500
（服务端自身的枚举缺失 bug），只能带 `?device_id=&status=` 过滤查询。

### 7.3 错误处理

- 任一步非预期状态码 → `XteinkUploadError`，附步骤名与响应体
- OSS policy 对 EPUB 有 `content-length-range` 下限（256 字节），生成阶段已校验
- content_type 由文件扩展名映射：`.epub → application/epub+zip`、`.txt → text/plain`

## 8. Web 管理界面（web/）

无用户系统。可选：`.env` 里设 `WEB_PASSWORD`，非空时对所有页面/接口启用 HTTP Basic Auth。

页面（HTMX 局部刷新）：

| 区域 | 功能 |
|---|---|
| 任务列表 | 每个 task：名称、下次执行时间、上次 run 结果徽标、启用开关、「立即执行」「编辑」「删除」 |
| 任务编辑 | 名称、选源（多选）、cron（输入框 + 常用预设按钮 + 人类可读预览）、summarize、format、first_run_lookback_hours |
| 源管理 | 每个 feed：url、full_text、use_proxy；「测试」→ 立即拉取，显示最新 5 条标题 + 每条全文提取成功与否 |
| 执行历史 | runs 倒序：时间、任务、条目数、文件名、状态、message；failed 的可「重跑」 |
| 设置 | 编辑 xteink / proxy / ai / fetch 段；「测试连接」分别验证：AI（primary/fallback 各发一个极短请求）、xteink（登录）、proxy（连通性） |

REST API：`/api/tasks`、`/api/feeds`、`/api/runs`、`/api/settings`、`/api/tasks/{id}/run`、`/api/feeds/{id}/test`、`/api/test/{ai|xteink|proxy}`。

配置写回：用 ruamel.yaml 保留注释；写回后调用 `scheduler.reload()` 重建 job。

## 9. 调度器（scheduler.py）

- 启动时读 config，为每个 `enabled: true` 的 task 用 `CronTrigger.from_crontab(task.schedule)` 注册 job
- job 函数 = `pipeline.run_task(task_id)`
- `reload()`：移除全部 job，按当前 config 重新注册（配置变更后调用）
- 同一 task 不并发执行（`max_instances=1`，`coalesce=True`）；`run_now` / `submit` 也必须遵守这一不变量（P4：`run_now` 在 task 已 active 时返回 `skipped`）
- misfire：使用内存 jobstore，容器停机期间错过的运行**不会**在重启后补跑（`next_run_time` 重新向前计算）。可接受：`first_run_lookback_hours` + `seen_items` 窗口内重试机制保证下一次调度运行仍会拾起未推送条目。`misfire_grace_time=None` 只影响存活进程内的 misfire（例如一次较长的 reload pause）

## 10. 部署

```
Dockerfile         —— python:3.12-slim，装依赖，CMD python -m push2xteink
docker-compose.yml —— 单服务，挂载 ./data:/data，暴露 Web 端口（默认 8080），
                      环境变量 CONFIG_PATH=/data/config.yaml、DB_PATH=/data/state.db、
                      WEB_PASSWORD（可选）
```

首次启动若 `config.yaml` 不存在 → 写一份带注释的样例并提示用户填写。

**P3 现状**：`python -m push2xteink` 目前**必须带子命令**（`run <task_id>` / `list`），无子命令时打印友好提示并 `exit 2`。容器入口的 `serve`（Web + 调度器）在 **P4** 落地，届时 `serve` 成为**默认子命令**（`CMD python -m push2xteink` 不带参数即启动常驻服务）。

## 11. 测试策略（TDD）

| 层 | 方法 |
|---|---|
| config / models | 样例 yaml 往返；校验错误（未知 feed id、非法 cron、summarize 缺 ai）；默认值 |
| state | 内存 SQLite；去重；首次执行窗口；未推送条目窗口内重试、超期放弃；pushed_at 标记时机 |
| feeds | 本地 RSS xml fixture；respx mock；guid 缺失回退 link |
| extract | 本地 HTML fixture；提取成功 / 正文过短回退 / 抓取超时回退 |
| summarize | respx mock；primary 成功 / primary 失败→fallback 成功 / 两者都失败→跳过 |
| builders | 生成 EPUB 后用 ebooklib 读回校验章节数与标题；txt 结构；EPUB 最小尺寸 |
| xteink | respx mock 三步 + OSS + 登录；校验 md5 / 表单字段 / content_type 映射 / 401 重登 / instant_upload 跳过步骤 B；不打真实接口 |
| pipeline | 全 mock 端到端：单源失败不影响其它源；上传失败不写 pushed_at；无新条目 → skipped |
| web | FastAPI TestClient：CRUD、`/tasks/{id}/run` 触发、Basic Auth 开关 |

手动冒烟（非 CI）：`scripts/smoke_xteink.py` —— 真实登录 + 上传一个小 TXT，确认线上接口未变。

## 12. 待实现时确认的小项

- EPUB 章节内的图片：先不下载内嵌，保留/剥离 `<img>` 待实现时定（倾向剥离，e-ink 意义不大）
- `qps` 限流的实现（简单 sleep 间隔即可）
- Web 端口、默认 lookback 等常量的最终取值
- 配置级 `timezone` 字段（让 cron 表达式按指定时区解释）留作 P6 之后的增强；当前所有 cron 按容器时区（P6 用 `TZ` 环境变量固定，默认 UTC）解释。P5 已把 UI 的时间显示统一到服务器时区并在页面上标注时区名（`_fmt_ts` / cron 预览），不再混用 UTC 与本地时钟。

## 13. P1 实现后补充（终审发现，供 P2–P6）

- **配置注释保留（修订第 8 节措辞）**：`write_config` 现按段落级 key 重写，只保证**文件头注释和段落注释**保留；`feeds:` / `tasks:` 列表项的**行内联注释在整体重写时会丢失**。P1 已通过 `extra="forbid"`（未知 key 在 load 阶段报错）堵住「网页保存抹掉手写内容」的数据丢失风险。**P5 增加一个任务**：改为递归合并进现有 `CommentedMap`、只增删列表项、原地改叶子标量，以完整保留注释。
- **APScheduler 星期字段**：`CronTrigger.from_crontab("... 0")` 中 `0` 是**周一**（APScheduler 3.x），非标准 cron 的周日。校验与执行都用同一 `CronTrigger` 所以自洽；但 P4/P5 的「人类可读预览」必须按 APScheduler 语义生成，否则会与用户查的 crontab 手册不一致。P4 决定：要么在文档里标注，要么在校验层把星期字段归一化成标准 cron。
- **`DEFAULT_PROMPT` 与 P2c 的契约**：当前 `DEFAULT_PROMPT` 只是一句指令，没有文章正文占位符。P2c 规划时定清楚 `summarize.py` 如何拼装（system message？`prompt + "\n\n" + 正文`？），并在 prompt 模板里显式留正文位置。
- **`seen_items` 无限增长**：**决定不做**：`seen_items` 每行约 120 字节，10 个源约 9 MB/年，`is_item_pushable` 走主键索引不随表增长变慢；按时间清理会让仍在 feed 里的旧文章重新变成「新」被再次推送，得不偿失。若某天真的需要，按「每源保留最近 N 条已推送行」而非按时间。（P6 曾加过按时间的 daily prune，终审发现它破坏「推过一次永不再推」不变量，已整体 revert。）
- **`State` 已线程安全**：`check_same_thread=False` + WAL + `busy_timeout=5000` + 每方法一把 `threading.Lock`。P3/P4/P5 可安全在多线程（FastAPI 请求线程 + APScheduler worker）共享同一个 `State` 实例，无需每线程新建。
- **数值下限已在模型层强制**：`qps` / `timeout_seconds` / `concurrency` / `first_run_lookback_hours` 均 `> 0`，`max_retries >= 0`。P2c 的 `1/qps` 限流器不会遇到 0。
- **`runs.status` 有 DB CHECK 约束**：只接受 `running|success|skipped|failed`。`finish_run` 的 `status` 参数是 `Literal`。注意 CHECK 只对新建 DB 生效，P1 尚无迁移机制——后续若改 `_SCHEMA` 需要迁移策略。

## 14. P2 实现后补充（整体复审发现，供 P3–P6）

- **HTTP 客户端统一走 `push2xteink.http.make_client`**：`trust_env=False`（绝不读环境 `HTTP(S)_PROXY`，代理只能显式传），共享模块级 `ssl.SSLContext`（certifi CA），固定 UA，`follow_redirects` 默认 True。P3 及后续所有 httpx 调用都用它，不要直接 `httpx.Client(...)`。每次新建 client 在有环境代理的机器上约 0.43s，这是唯一原因让 P2 测试从 37s 降到 2s。
- **`Summarizer` / `XteinkClient` 有生命周期**：各自持有长期 httpx client，提供 `close()` / `__enter__` / `__exit__`（+ `__del__` 兜底）。**P3 应在每次 config reload 时 `close()` 旧实例、重建新实例**，而不是每个 run 新建。两者跨线程共享安全（`Summarizer` 靠内部锁，`XteinkClient` 靠 P1 线程安全的 `State`）。
- **P3 用 `push2xteink.builders.common.html_to_text(html) -> str`** 把 `Article.content_html` 转成纯文本喂给 `Summarizer.summarize(text)`。不要把原始 HTML 直接给 LLM。
- **`Article.published_at` 时区不变量未在类型层强制**：`feeds.fetch_feed` 产出的都是 aware UTC，但 `models.Article.published_at: datetime | None` 没有 aware 约束；`select_new_articles` 对 naive `now` 有防护、对 naive `published_at` 没有；`builders.format_published` 对 naive 会静默按本地时区偏移。**P1 follow-up**：把字段改成 `pydantic.AwareDatetime | None`，一次性关掉这一类。
- **`ProxyConfig.url` 无 scheme 校验**：`http://` / `socks5://` 前缀写错（如 `127.0.0.1:7890`）在 P2 只会在运行时被各模块捕获成 per-feed 警告（已加 `ValueError` 到 guard）。**P4/P5 在 config load 阶段校验 scheme**，让用户改配置时立刻报错。
- **`xteink` 上传对非预期响应体已全部收敛为 `XteinkUploadError`**：login/signature/callback 三处 JSON 解析都过 `_json_dict`（非 JSON body、非 dict body、缺字段、`success:false` 都抛 `XteinkUploadError`）。P3 的失败隔离 `except XteinkUploadError` 可靠。
- **容器时区必须显式固定（P6 硬性要求）**：`CronTrigger.from_crontab(expr)` 按调度器时区（= 机器本地时区）解释表达式。P6 的 `docker-compose.yml` 必须设 `TZ`（默认 `UTC`）；若 `TZ` 取非 UTC 值，镜像里必须装 `tzdata`，否则 APScheduler 在启动时就会因找不到时区数据抛异常。P5 的 UI 已统一按服务器时区显示并标注时区名（见第 12 节）。
- **已知延后的小项**（不阻塞 P3）：`Summarizer` 重试无退避（默认 qps=1 时恰好有 1s 间隔）；`safe_filename` 按码点而非字节截断（超长 CJK 任务名可能超 255 字节 `NAME_MAX`）；`select_new_articles(state, feed_id, articles, ...)` 的 `feed_id` 参数与 `Article.feed_id` 冗余。

## P3 实现后补充（整体复审发现，供 P4–P6）

- **I5 —— `Pipeline.close()` 与在飞的 `run_task` 竞态**：`close()` 会关掉 `Summarizer` / `XteinkClient` 的 httpx client；若某个 `run_task` 正在跑，config reload 时直接 `close()` 旧实例会让在飞的 run 报 `ClientClosed`。**P4 硬性要求**：reload 流程为 `scheduler.pause()` → 等待活跃 job 排空（`ThreadPoolExecutor` join / APScheduler `get_jobs` 轮询）→ `close()` 旧 `Pipeline` → 用新 config 重建 → `scheduler.resume()`。P3 的 CLI 单跑模式无此问题（进程内只有一个 run）。
- **M3 —— 超长非 ASCII `task.name` 撑爆 `NAME_MAX`**：`_build` 的标题 `f"{task.name}_{now:%Y%m%d}"` 经 `safe_filename` 按码点截断；30+ 个 CJK 字符的任务名 UTF-8 编码后可能超 255 字节。**P1 follow-up（models.py）**：给 `Task.name` 加 `Field(..., max_length=60)`，在 config load 阶段就报错。属 P1 改动，此处仅记录。
- **M8 —— 极短正文（RSS 摘要）跳过 AI 摘要**：不少 feed 的 `content_html` 本身就是一两句话的 blurb，再喂给 LLM 摘要既费钱又无收益。P4/P5 可在 `_prepare` 里对 `html_to_text` 后长度低于阈值（如 200 字）的文章直接跳过 `summarize`。成本优化，非正确性问题。
- **APScheduler worker 池上限**：每个 job 内部 `_prepare` 会各自 fan out 一个 `ThreadPoolExecutor(fetch.concurrency)`。P4 配置调度器时必须把 executor 的 `max_workers` 压到 3–5，否则 N 个任务并发时线程数是 `N × concurrency`，会打爆下游 feed / AI 接口的限流。

## P4 实现后补充（整体复审发现，供 P5–P6）

- **P5 不要扩展 `_serve` 的 watch loop**：把它抽成一个 `_ConfigWatcher(path, on_change)` daemon 线程 helper（uvicorn 拥有主线程并安装自己的 SIGINT/SIGTERM）。P5 FastAPI lifespan：startup 构建 State + Scheduler + 启动 watcher；shutdown 停 watcher → `sched.shutdown()` → `state.close()`。
- **`Scheduler.run_now` 是同步的**（阻塞调用方整个 pipeline run，数分钟）。网页「立即执行」按钮需要一个 fire-and-forget `Scheduler.submit(task_id)`，通过 APS executor 以一个独立 job id（`f"manual:{task_id}"`，**不是** `task_id`——`replace_existing` 会覆盖 cron job）派发，从而共享 4-worker 上限与 APS instance 计数。`run_now` 保留给 CLI / 测试。
- **P5 网页保存 `config.yaml` 的 handler 应让 watcher 去 reload**（≤5s 延迟），不要直接调 `sched.reload()`——否则 watcher 会在 mtime 变化时再 reload 一次（双重 pause+drain）。
- **P6 Dockerfile**：`ENV PYTHONUNBUFFERED=1`，让 `print()` 与日志实时写到 `docker logs`；`docker-compose.yml`：`stop_grace_period: 150s`（web 入口用 `_WEB_DRAIN_TIMEOUT = 25s` 优雅排空，见 `web/app.py`；旧文写的 120s 是 CLI `_serve` 的值，容器跑的是 web 入口。Docker 默认 10s 会在 `push_file` 中途 SIGKILL——按 mark_pushed-after-upload 不变量是安全的，只是吵）。
- **misfire / 内存 jobstore**：见第 9 节修订——容器停机错过的运行不补跑，靠 lookback + `seen_items` 重试窗口兜底。
- **已应用的复审修复**（本分支）：C1 reload 工厂异常不再 brick 调度器（`_serve` watch loop 兜底 `except Exception` 打印 `reload failed`）；I1 reload drain→swap 竞态（`_run` 在 `_cond` 下与注册原子地捕获 pipeline 引用，swap 也在同一把锁下）；I2 `reload()` / `shutdown()` 由 `threading.RLock` 串行化；I3 `run_now` 遵守非并发不变量并在 shutdown 后拒绝；M2 `shutdown` 改 `aps.shutdown(wait=False)` + 单次 bounded `_drain`（`drain_timeout` 也覆盖 cron job）；I5 `_serve` 顶部 `logging.basicConfig`；M1 mtime token 用 `(st_mtime_ns, st_size)`；M6 `start()` 失败时 `sched.shutdown()` 关掉已建 Pipeline 的 httpx client；M8 在 `load_config` 前读 mtime；M9 `start()` 后记录各 job 的 next run time。新增 P5-enabling accessor：`Scheduler.config`（property）、`Scheduler.next_run_time(task_id)`。
- **延后**：M4（第二个信号升级为硬退出）、M5（生产信号路径无测试）——留给 P5 lifespan 改造。`seen_items` 定期 prune：见第 13 节，**决定不做**。

## P5 实现后补充（整体复审发现，供 P6）

- **已应用的复审修复**（本分支）：C1 非并发保证下沉到 `_run`（cron fire + `submit()` 的 `manual:` job 是两个 APS job id，`max_instances=1` 管不到，此前会重复推送同一份文件）；C2 Basic Auth 改成 `BaseHTTPMiddleware`（`FastAPI(dependencies=[...])` 只覆盖 `APIRoute`，`/openapi.json` `/docs` `/redoc` `/static/*` 此前完全裸奔），`/healthz` 显式豁免以便 Docker HEALTHCHECK；I1 列表项之间的独立注释不再漂移（恒等往返已可字节级一致，重排序时注释跟着条目走）；I2 `apply_config_change` 全程持 `app.state.config_lock`；I3 config token 在 `reload()` 之前 priming、失败时 `invalidate_config_token()`，web 侧 `Scheduler` 用 `drain_timeout=25.0`；I4 时间显示统一到服务器时区并标注；I5 `Feed.url` 校验 http(s) scheme；I6 跨源写请求（`Origin` host ≠ `Host`）一律 403；I7 `ConfigError` 不再内嵌 pydantic 的 `input_value`（含明文密码 / api_key）；I8 校验失败时回填用户提交值；I9 `ai.fallback` 可在网页增删；I10 `write_config` 保留原文件权限位（新文件 0600）；首次启动写样例 config 并退出 2。
- **`/healthz` 是未鉴权端点**：即使设了 `WEB_PASSWORD` 也可匿名访问（只暴露「进程活着」）。P6 的反向代理若要收紧，自行在代理层限制来源。
- **`_task_row_view` 每行一次 `last_run_for_task` 查询**：任务列表渲染是 N+1 次 SQLite 查询。任务数是个位数量级，暂不优化；若 P6 后任务数增长，改成一次 `GROUP BY task_id` 的批量查询。
- **`api_settings.test_xteink` 直接调用 `XteinkClient._access_token`**：探活复用了私有方法。若 P6 重构 `xteink.py`，需要一个公开的 `ping()` / `login()` 入口。
- **`hx-confirm` 删除确认无测试**：纯浏览器行为，TestClient 覆盖不到；留给手动冒烟。
- **`seen_items` 定期 prune**（见第 13 节）：**决定不做**——按时间清理会重新推送仍在 feed 里的旧文章。
- **`proxy.url` 不做脱敏**：不在规范的脱敏清单里（脱敏只覆盖 `xteink.password` 与 `ai.*.api_key`）。若代理 URL 里带凭据，属已知取舍。
