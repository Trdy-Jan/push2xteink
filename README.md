# push2xteink

把 RSS 源里的新文章自动抓取、（可选）AI 摘要、打包成 EPUB/TXT，再上传到你绑定的
阅星曈 X4 阅读器。单进程 Docker 容器：Web 管理界面 + APScheduler 调度器 + 执行流水线
跑在同一个进程里，状态存本地 SQLite。

## 架构总览

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

## 快速开始

```bash
git clone <repo-url> push2xteink && cd push2xteink
mkdir -p data
cp .env.example .env
printf 'APP_UID=%s\nAPP_GID=%s\n' "$(id -u)" "$(id -g)" >> .env   # 让容器以当前用户跑，data/ 保持可编辑
# 按需改 .env 里的 PORT / WEB_PASSWORD / TZ

docker compose up -d
# 首次：容器把样例写进 data/config.yaml、以退出码 2 退出一次，然后重启并按样例正常启动。
# Web UI 立即可访问 http://<host>:<PORT>/。
$EDITOR data/config.yaml     # 填 xteink 手机号/密码、(可选) AI 接口，加 RSS 源和任务
chmod 600 data/config.yaml   # 明文密钥，见「安全须知」
# 保存后 ~5s 内热加载 —— 无需 restart。也可用 Web 的 Settings 页改。
```

**首次 `docker compose up` 的实际行为：** `_serve` 发现没有 `config.yaml`，写入模板后
退出 2 **一次**（`docker compose logs push2xteink` 能看到
`wrote a sample config to /data/config.yaml — fill it in and restart`），`restart: unless-stopped`
随即重启 —— 这次容器**正常起来并按模板提供服务**：Web UI 在 `http://<host>:<PORT>` 立即可访问，
`morning-brief` 这个示例 cron 任务也已注册。它**不会**进入重启循环 —— 前提是容器能写 `./data`
（见上面快速开始的 `APP_UID` 步骤，或下方「非 root 用户 + bind mount 权限」）；否则每次都写不了
配置、退出 2、被重启，就成了真正的循环。

在你把真实的 xteink 手机号/密码填进 `data/config.yaml` 之前，唯一坏掉的是**任务执行**：
定时或手动触发的 run 会在最后的 xteink 上传步骤因登录失败而报错 —— 在 `/runs` 页面和
`docker compose logs` 里能看到。配置改好后 ~5s 内热加载，下一次 run 即正常。

### 非 root 用户 + bind mount 权限

容器以 `.env` 里的 `APP_UID:APP_GID`（默认 `10001:10001`，非 root）运行，需要能写 `./data`
（`config.yaml` / `state.db` / `config.yaml.tmp` / `state.db-wal` 都在这）。宿主目录权限接管
bind mount。快速开始里的 `printf 'APP_UID=%s\nAPP_GID=%s\n' "$(id -u)" "$(id -g)" >> .env`
把它设成你自己的 uid，所以 `./data` 和 `config.yaml` 一直是你可编辑的。

如果跳过那一行（用默认 10001），需要 `sudo chown -R 10001:10001 ./data`，之后编辑
`config.yaml` 也得 `sudo`。

## 环境变量

`.env`（compose 自动读取）里可配的：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `8080` | 对外映射端口（`${PORT}:8080`）。容器内部始终监听 8080。 |
| `WEB_PASSWORD` | 未设 | 设了则 Web 界面走 HTTP Basic Auth（用户名任意）。未设 = **无鉴权**。`/healthz` 永远豁免。 |
| `TZ` | `UTC` | 容器时区。所有 cron 表达式按它解释。改了要 `docker compose up -d` 重建。 |
| `APP_UID` / `APP_GID` | `10001` | 容器进程的 uid/gid（`user:` 传入）。设成 `id -u` / `id -g` 让 `./data` 保持宿主可编辑。 |
| `CONFIG_PATH` | `/data/config.yaml` | 配置文件路径（compose 已固定为卷内路径，一般不用改）。 |
| `DB_PATH` | `/data/state.db` | SQLite 路径（同上）。 |

## 配置文件

所有源、任务、xteink 账号、AI 接口都在 `data/config.yaml`（字段说明见
`docs/superpowers/specs/2026-08-31-push2xteink-design.md` 第 4 节）。Web 界面读写的是
同一个文件：手工编辑保存后 5 秒内热加载，界面里点「保存」立即生效，无需重启容器。

## 安全须知

- **明文密钥**：`config.yaml` 里存的 xteink 密码和 AI api_key 都是明文。务必
  `chmod 600 data/config.yaml`，并确保宿主机上这个目录不被其他用户读到。
- **不设 `WEB_PASSWORD` 时没有任何鉴权** —— 任何人访问到端口就能改配置、触发任务。
  **不要把端口直接暴露到公网。** 需要公网访问就设 `WEB_PASSWORD`，并在前面套一层
  HTTPS 反向代理（Basic Auth 是明文传输的）。
- **跨源写请求会被拒绝**（CSRF 防护）：**带 `Origin` 头且跨源**的写请求会被拒绝（403）；
  不带 `Origin` 头的写请求会被接受（这也是 `curl -X POST` 冒烟能通过的原因）。
  用反代时把 `Host` / `Origin` 头透传正确。

## 时区与 cron

- 任务的 `schedule` 是标准 5 段 cron，按容器 `TZ` 解释（默认 UTC）。
- **APScheduler 的星期编号和标准 crontab 不同：`0` = 周一 … `6` = 周日。**
  任务编辑页的「预览」会显示接下来几次真实触发时间，以它为准。

## 运维

- **单进程单 worker**：调度器在进程内，跑多个 worker 会重复推送。`serve` 不接受
  `--workers`，别自己加。
- 日志：`docker compose logs -f push2xteink`，能看到调度注册、每次任务执行的结果。
- `state.db` 存执行历史（`runs`）和去重记录（`seen_items`），随 `./data` 卷持久化。
  `seen_items` 只增不删（每行约 120 字节，10 个源约 9 MB/年）—— 按时间清理会让仍在
  feed 里的旧文章重新被当成「新」再次推送，所以刻意不做。
- **优雅停止**：`docker compose stop`。SIGTERM 触发 ~25s 优雅排空；更长的推送会被安全切断
  （上传只在阅读器确认后才标记完成，不会留下半截状态）。`stop_grace_period: 150s` 只是余量。

## 本地开发（非 Docker）

```bash
pip install -e ".[dev]"
python -m pytest -q

# CLI（子命令：list / run / serve）
python -m push2xteink --config data/config.yaml --db data/state.db list
python -m push2xteink --config data/config.yaml --db data/state.db run <task_id>
python -m push2xteink --config data/config.yaml --db data/state.db serve   # web + 调度器，阻塞
```

`--config` / `--db` 不传时分别取环境变量 `CONFIG_PATH` / `DB_PATH`，再退回
`data/config.yaml` / `data/state.db`。不带子命令等价于 `serve`。

## 验证部署

Docker 环境下的手动冒烟（本仓库开发环境没有 Docker，以下未执行，供你部署后自查）：

```bash
docker compose build
docker compose up -d
sleep 8
# 首次：容器写入模板、退出 2 一次，然后重启并正常启动（按模板服务）
docker compose logs --tail 20 push2xteink       # 期望能看到：wrote a sample config to /data/config.yaml
docker compose ps                                # 期望 STATUS 为 Up（不是反复 Restarting）

# 填好 data/config.yaml 后（热加载，不必 restart；下面 sleep 等热加载生效）：
sleep 6
curl -fsS http://localhost:8080/healthz                              # {"ok":true}（永远豁免鉴权）
# 设了 WEB_PASSWORD 时，非 /healthz 路由需带 -u；用户名任意：
curl -s -o /dev/null -w '%{http_code}\n' -u :"$WEB_PASSWORD" http://localhost:8080/   # 200
docker compose exec push2xteink \
    python -m push2xteink --config /data/config.yaml --db /data/state.db list

# 触发一次任务，看阅读器收到文件（设了 WEB_PASSWORD 时同样要带 -u）：
curl -s -X POST -u :"$WEB_PASSWORD" http://localhost:8080/api/tasks/<task_id>/run   # 202
docker compose logs -f push2xteink                                   # 看 run 结果

# 优雅停止计时（无任务在跑时应很快；有长推送时最多 ~25s 排空）：
time docker compose stop
```
