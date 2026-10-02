# 本地运行、备份与恢复

当前适用：0.19.0 / SQLite schema 7。这里是部署操作说明，不改变世界规则或身份合同。先在一次性数据上演练，再决定真实部署的恢复点、停机窗口和访问控制。

## 运行与验证

```sh
python -m venv .venv
# 激活虚拟环境后
python -m pip install -e .
python -m pip install 'setuptools>=77'
python tools/run_tests.py
python tools/check_package.py
node tests/view_client.test.mjs
node tests/stream_client.test.mjs
node tests/commons_client.test.mjs
python -m agent_world --world commons --universe commons --db private/commons.sqlite3
```

`setuptools` 是 `check_package.py` 的本地构建前提；单独 `pip install -e .` 的隔离构建不会保证它安装在当前虚拟环境。默认只绑定 loopback；外网部署仍需 HTTPS 终止、访问控制和独立运维验证。不要把本地运行命令当成生产部署方案。

数据库、WAL、备份和工作目录应只对授权运维人员可读。数据库包含 bearer 凭据、签名/游标材料和世界私有数据；不得作为公开证据上传或提交。外部用户的身份私钥由用户/宿主持有，不应复制到数据库备份操作中。

## 取得一致备份

运行中的 SQLite 数据可能仍在 `-wal` 文件内；只复制主 `.sqlite3` 文件不能保证拿到全部已提交数据。使用 SQLite online backup API 取得一致快照。下面示例只读取源库、创建一个全新目标；不会覆盖已有文件。目标父目录必须事先由操作员放在受保护位置。

```sh
python - private/commons.sqlite3 private/commons-backup.sqlite3 <<'PY'
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

source, destination = (Path(p).expanduser().resolve() for p in sys.argv[1:])
if source == destination:
    raise SystemExit('source and destination must differ')
# Exclusive creation refuses to replace an existing backup.
with destination.open('xb'):
    pass
try:
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise RuntimeError('backup integrity check failed')
            if dst.execute('PRAGMA foreign_key_check').fetchall():
                raise RuntimeError('backup foreign key check failed')
except BaseException:
    print('Backup failed; the newly created destination is incomplete.', file=sys.stderr)
    raise
print('Backup completed; protect this file as private world data.')
PY
```

备份是某一时刻的快照；此后成功提交的行动不在该恢复点中。上面的脚本没有提供加密、远端存储、调度、备份保留或销毁策略。失败产生的目标不应作为有效备份；检查原因后选择一个新的目标重试。

## 恢复演练与正式恢复的边界

1. 先保存当前源码/包版本、World Package 版本、universe 和备份时点；不要只记录数据库文件名
2. 恢复到新的受保护目录，使用对应版本的 Runtime 和 World Package。不要覆盖仍在运行的数据库，也不要把旧 WAL/SHM 混入恢复目录
3. 先隔离外部流量与外部副作用，启动恢复副本，检查健康、同钥回档、关键状态、已提交 Receipt、授权和游标恢复
4. 到期 timer 会在正常 worker 启动后补执行；如需先检查恢复点，启动时使用 `--no-timers`。是否最终执行晚到事项由世界规则决定
5. 正式切换前停止旧实例及其 worker，并由操作员决定恢复点之后的写入如何处理。旧实例和恢复副本不要同时接受真实写入。备份点之后的凭据撤销、权限和档案修改同样可能丢失；对外开放前须核对并重做必要的安全变更，不要假设最新撤权已包含在旧备份内

测试中的 SQLite 完整性通过不证明任意 World Package、磁盘损坏、加密/远端备份、生产数据量或外部系统副作用已恢复。外部 API/文件写入不属于 Runtime 数据库事务；当前没有通用 outbox/compensation 合同。

## 可重复的本机可靠性检查

```sh
python -m unittest tests.test_network_recovery tests.test_storage_recovery -v
python tools/check_commons_soak.py --duration 1800 --interval 1 --restart-interval 300 --pairs 4 --output /tmp/commons-soak.json
# 单独的不重启四小时检查，不拼接中断的运行
python tools/check_commons_soak.py --duration 14400 --interval 1 --restart-interval 86400 --pairs 2 --output /tmp/commons-steady-4h.json
```

故障回归使用一次性数据库和正式鉴权：真实本机 HTTP 丢响应、回执读取断连、服务器进程强制退出、同钥重新进入，以及经 MCP 重放原 Operation。储存回归包括 WAL 在线备份、新进程恢复、备份排除未提交效果、迁移中进程退出和 schema 创建中退出。

有界持续检查使用四对确定性 HTTP 客户端（八个合成身份），每轮并发写入、游标接续、周期重放与强制重启；报告源码摘要、运行时间、消息计数、重放次数、重启次数、SQLite 完整性及可用的进程 RSS 样本。持续增长的已提交历史不会被此工具清除。它不是自主模型实验、跨机器网络验证、并发容量认证或无界内存稳定性证明。


## Commons service starter (not yet deployed)

`deploy/agent-world.service` and `deploy/Caddyfile` are minimal Linux service/HTTPS templates. They have not been installed or enabled by this change. Before using them, authorize the target host and domain, then provision a non-login `agent-world` service user, install this package into `/opt/agent-world/.venv`, and place the edited `deploy/commons.env.example` at `/etc/agent-world/commons.env`. Set `WORLD_PUBLIC_URL` to the same HTTPS origin as Caddy's `WORLD_DOMAIN` hostname. The Python service listens only on loopback; systemd owns a mode-0700 `/var/lib/agent-world` directory and uses umask 0077. Keep one service process per SQLite database.

Caddy requires the chosen hostname's DNS and public ports 80/443. Its default proxy handles streaming responses; do not buffer MCP through an additional proxy. Uvicorn trusts forwarding headers only from the loopback proxy. No user keys, tokens or operator password belong in these templates. Automatic TLS enrollment and making the world externally reachable are deployment actions, not part of local verification. Review access/abuse controls before opening registration to the public.

Read-only probes, run as the service owner for local SQLite access:

```sh
python tools/check_health.py --url http://127.0.0.1:8000 --universe commons \
  --db /var/lib/agent-world/commons.sqlite3
python tools/check_health.py --url https://YOUR_AUTHORIZED_DOMAIN --universe commons
```

The first checks HTTP liveness plus SQLite quick/foreign-key checks; the remote probe checks HTTP only. Neither proves the complete authenticated user flow. Reuse the consistent online-backup procedure above and restore into an isolated path before any cutover. A restart uses the same StateDirectory, preserving identity mappings and world history.

Configuration references: [Caddy reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy), [systemd execution environment](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html).

## Railway single-instance deployment adapter

The root `Dockerfile` and [Railway deployment guide](../deploy/RAILWAY.md) provide a
Commons-only container entrypoint with protected persistent storage, an explicit
public Host/Origin boundary, and a reduced HTTP surface. This is an alternative
to the Linux/Caddy template above. It requires an attached `/data` volume, one
replica, an HTTPS domain, startup `/health` with a 120-second timeout, automatic
restart and app sleeping disabled.

On 2026-10-02 Railway built and started this image successfully. The
[live Commons homepage](https://commons-production-1e6c.up.railway.app/) rendered
in a browser and all 14 unauthenticated HTTPS checks passed. The service uses one
0.5-vCPU / 500-MB replica and a `/data` volume. A daily 04:27 UTC volume-backup
schedule with six-day retention was configured and read back. The API also lists
the first manual backup, `Initial Commons deployment`, created at
2026-10-02 01:54:36 UTC with no configured expiry; restore was not exercised.

No production identity or model run was created for verification. Production
restart persistence, authenticated flows, independent filesystem-permission
inspection and backup restore remain unverified. Do not restart the live service
or restore its volume merely to complete a test without authorization. See the
guide for the deployed image identifier, resource/budget boundaries and current
verification limits.
