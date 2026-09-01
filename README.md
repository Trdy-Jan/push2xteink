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
                             ▼  三步上传
                    api-prod.xteink.cn → 阿里云 OSS → callback
                             │
                             ▼
                    绑定的阅读器（阅星曈 X4）
```

## 快速开始

```bash
git clone <repo-url> push2xteink && cd push2xteink
mkdir -p data
cp .env.example .env            # 按需改 PORT / WEB_PASSWORD / TZ

docker compose up -d            # 首次：容器会往 data/config.yaml 写一份样例，
                                # 然后以退出码 2 反复重启（日志里有明确提示）
$EDITOR data/config.yaml        # 填 xteink 手机号/密码、（可选）AI 接口，加 RSS 源和任务
chmod 600 data/config.yaml      # 里面是明文密钥，见下方「安全须知」

docker compose restart
# 打开 http://<host>:8080
```

首次 `docker compose up` 后容器会进入重启循环，这是正常的 —— `serve` 发现 `config.yaml`
还是样例（占位符没填）时会退出 2，`restart: unless-stopped` 让它一直重启直到你填好配置。
`docker compose logs push2xteink` 能看到 `wrote a sample config to /data/config.yaml — fill it in and restart`。

### 非 root 用户 + bind mount 权限

容器以 uid `10001`（非 root）运行，需要能写 `./data`（`config.yaml` / `state.db` /
`config.yaml.tmp` / `state.db-wal` 都在这）。宿主目录权限接管 bind mount，二选一：

```bash
# 方案 A：把宿主 data/ 的属主改成容器 uid
sudo chown -R 10001:10001 ./data

# 方案 B：让容器用当前用户跑（在 docker-compose.yml 的 service 里加一行）
#   user: "${UID}:${GID}"
# 然后 `UID=$(id -u) GID=$(id -g) docker compose up -d`
```

## 环境变量

`.env`（compose 自动读取）里可配的：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `8080` | 对外映射端口（`${PORT}:8080`）。容器内部始终监听 8080。 |
| `WEB_PASSWORD` | 未设 | 设了则 Web 界面走 HTTP Basic Auth（用户名任意）。未设 = **无鉴权**。`/healthz` 永远豁免。 |
| `TZ` | `UTC` | 容器时区。所有 cron 表达式按它解释。改了要 `docker compose up -d` 重建。 |
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
- **跨源写请求会被拒绝**（CSRF 防护）：只有同源的 `POST`/`PUT`/`DELETE` 被接受。
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
  `seen_items` 里已成功推送的记录 90 天后由内部 job 自动清理（每天 03:17，容器时区）；
  未推送的行（还在重试窗口内）永不删。
- **优雅停止**：`docker compose stop`（`stop_grace_period: 150s`）。容器收到 SIGTERM 后
  会等正在跑的任务收尾（最多 ~150s）再退出，避免半截的推送。

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
sleep 5
# 首次：应看到样例配置被写入 + 容器在重启循环
docker compose logs --tail 20 push2xteink       # 期望：wrote a sample config to /data/config.yaml

# 填好 data/config.yaml 后：
docker compose restart ; sleep 5
curl -fsS http://localhost:8080/healthz                              # {"ok": true}
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/      # 200
docker compose exec push2xteink \
    python -m push2xteink --config /data/config.yaml --db /data/state.db list

# 触发一次任务，看阅读器收到文件：
curl -s -X POST http://localhost:8080/api/tasks/<task_id>/run        # 202
docker compose logs -f push2xteink                                   # 看 run 结果

# 优雅停止计时（应接近任务收尾时间，不是立刻）：
time docker compose stop
```
