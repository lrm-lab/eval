# LRM Eval 前端展示与分析平台

基于 `lrm-lab/lrm-eval` 与 197（`LRM-02`）全量评测数据源构建的统一评测展示、对比与细粒度诊断平台。

## 权威设计文档
- [v2.2.1 终审规划文档](docs/planning/v2.2.1/README.md)

## 目录结构（三区存储隔离）
- `/home/share-data/eval/`（CIFS 共享盘，22T）：协作源码、`config/` 配置、`docs/planning/` 规划文档与构建缓存。
- `${LRM_RUNTIME_DIR:-$HOME/.local/share/lrm-eval}`（197 本地 ext4 `0700` 独占目录，严守 ≤ 750 MB 预算）：
  - `bin/uv` 与精简 Python 3.12 `venv/`
  - `releases/current`（由 `backend/scripts/deploy_release.sh` 部署的受管本地不可变发布副本，目录 `0750`、文件 `0640`、`source_approval_registry.yaml` 严格 `0600`，最多保留 2 版，附带 `RELEASE_MANIFEST.json` SHA-256 清单）
  - `state/`（`0700`）：`eval_platform.db`（SQLite WAL + FTS5）、`secrets.env`（`0600`）、轮转日志与最新 1 份备份
  - `uploads-staging/`（`0700`，Fail-Closed POSIX 权限探针验证）
  - `evidence-store/`（`0700`，受限证据存储隔离区）
  - `runner-work/`（`0700`，隔离执行工作区）

## 低水位写保护与 SQLite 只读 / Sidecar 契约（`LOCAL_DISK_LOW`）
- 当状态存储或密钥所在挂载分区剩余空间 `< 1.5 GiB`（或本地运行目录超过 `750 MB`）时，写守卫对所有写入口 Fail-Closed 返回 `503 LOCAL_DISK_LOW`：包括新库建表、密钥新建/残缺覆盖/轮换（按 `secrets_file` 实际挂载点检查）、启动扫描、后台热/冷扫描、手动重扫、`taxonomy/reload`、对比会话写入及 `init_admin.py`。
- 已有数据库在低水位下以严格只读模式（`read_only_mode=True`）提供只读查询与 `/admin/health`：
  - **无非空 `-wal` 时**：以 `file:<db>?mode=ro&immutable=1` 打开，保证**零 `-wal`/`-shm` sidecar 文件创建**；
  - **非空 `-wal` 与 `-shm` 均已存在时**：以 `file:<db>?mode=ro` 打开读取现有 WAL，不新建 sidecar；
  - **仅存在非空 `-wal` 而缺少 `-shm` 时**：直接 Fail-Closed 返回 `503 LOCAL_DISK_LOW`，绝不在低水位下隐式新建 `-shm`。

## 部署与验收（M0–M3）

在服务账号下执行。运行目录使用本地 ext4，协作源码、npm 缓存和脱敏导出使用共享盘。部署前确认账号没有加入 `docker` 组，且本地挂载至少剩余 1.5 GiB。全套命令使用同一个绝对路径 `LRM_RUNTIME_DIR`。

```bash
export LRM_EVAL_ROOT="$(pwd -P)"
export LRM_RUNTIME_DIR="${LRM_RUNTIME_DIR:-$HOME/.local/share/lrm-eval}"
export LRM_RUNNER_SOCKET_PATH="$LRM_RUNTIME_DIR/runner-work/runner.sock"
export LRM_RUNNER_IPC_TOKEN_FILE="$LRM_RUNTIME_DIR/state/runner_ipc.token"

bash backend/scripts/bootstrap_env.sh
bash backend/scripts/build_frontend.sh

# 交互式创建或更新管理员，按提示输入密码。
"$LRM_RUNTIME_DIR/venv/bin/python" backend/scripts/init_admin.py \
  --username admin --display-name "Administrator" --projects default

# 生成或复用本地 TLS 证书，部署本地发布副本并启动 Web 和 HTTPS 代理。
bash backend/scripts/start_https_demo.sh --start
bash backend/scripts/start_https_demo.sh --status

# 在同一组环境变量下，用独立受管进程启动 Runner。
bash "$LRM_RUNTIME_DIR/releases/current/backend/scripts/run_runner.sh"
```

- `init_admin.py` 也支持由部署环境注入 `LRM_ADMIN_PASSWORD`，适合非交互运行。同名账号会更新密码、角色、项目和权限。按实际项目调整 `--projects`。
- HTTPS 入口默认是 `https://10.110.158.197:8443`，FastAPI 监听 `127.0.0.1:8680`。通过 `LRM_DEMO_HOST_IP`、`HTTPS_PORT` 和 `LRM_ALLOWED_ORIGINS` 配置实际入口。演示证书位于 `state/tls/server.crt`，在客户端核对 `--status` 显示的指纹后再信任证书。停服使用 `start_https_demo.sh --stop`，并在进程管理器中单独停止 Runner。
- 已有 HTTPS 入口的环境可使用 `deploy_release.sh` 安装发布副本，再由进程管理器运行副本中的 `run_server.sh` 和 `run_runner.sh`。HTTPS 代理需转发协议头并关闭 SSE 响应缓冲。升级前备份，重启 Web 与 Runner 后再验收。需要回滚时从上一个已校验的冻结包重新部署。
- Runner 使用 `0600` Unix socket 和令牌，原始 `--work`、待审上传和受限证据目录保持 `0700`。网关配置在受管发布副本，凭据通过服务账号的 `state/gateways.env`（`0600`）注入。在 `/admin/health` 检查 `isolated_runner_ready=true`、正数 `runner_pid`、`docker_group_attached=false` 和 `managed_framework` 探针。
- Harbor Gateway 可通过 Runner 服务的 `LRM_RUNNER_OPENENV_PYTHON` 指向安装 OpenEnv 采集及 Harbor 依赖的绝对 Python 路径。Runner 在启动时用该解释器检查采集导入，Docker、受管框架与导入均通过才开放 Gateway。更新依赖环境后重启 Runner 以重新检查。
- OpenEnv 上游使用机构证书时，在 Runner 服务环境设置 `SSL_CERT_FILE`，指向包含该机构 CA 的可信 PEM 文件，例如已安装机构 CA 的 `/etc/ssl/certs/ca-certificates.crt`。

运行 API 回归时将 pytest 的缓存和临时文件放在本地临时目录。197 样本验收需要能读取规划文档所列共享数据源。

```bash
qa_tmp="$(mktemp -d /dev/shm/lrm-eval-qa.XXXXXXXX)"
"$LRM_RUNTIME_DIR/venv/bin/pytest" backend/tests \
  --basetemp="$qa_tmp/work" -o "cache_dir=$qa_tmp/cache" -v
```

M3 浏览器验收使用已安装 Playwright 与 Chromium 的 Node 22 环境。将 `frontend/e2e/verify_m3_e2e.mjs` 放在该环境可解析 `playwright` 模块的位置，使用 `BASE_URL`、`TEST_USER`、`TEST_PASS` 指定隔离测试站点与账号，然后运行 `node verify_m3_e2e.mjs`。测试站点应启用外部 Runner，并为默认 Direct smoke 网关设置几秒响应延迟，以便在首条试题结果前中断 SSE。浏览器会实际提交评测并核对续传、对账批次、stdio 展示和 CLS。OPD 空态验收使用空的 `training-runs/`。如果 Chromium 无法连字体 CDN 而 `curl` 可用，可设置 `E2E_FETCH_FONTS_WITH_CURL=1`，由 `curl` 获取同一份字体资源。

## 冻结包与校验

在 Linux 上使用 Git、Node 22、npm 与 GNU tar/coreutils。冻结包由指定提交中的受版本控制文件和干净构建的 `frontend/dist` 组成。脚本在两个独立源码目录中分别执行 `npm ci` 和生产构建，逐文件比较输出。默认工作目录位于 `/dev/shm`，可通过 `TMPDIR` 调整。需要复用依赖下载时可设置可写的 `LRM_SNAPSHOT_NPM_CACHE`。重复打包使用相同 Node/npm、平台及构建环境。

```bash
bash backend/scripts/package_snapshot.sh HEAD /path/to/lrm-eval.tar.gz
```

发布记录保留脚本输出的 `GIT_COMMIT`、`REPRO_BUILD_BYTE_IDENTICAL=True`、`TREE_SHA256` 和 `TAR_SHA256`。先检查压缩包哈希，再解包并重算文件树哈希。

```bash
archive=/path/to/lrm-eval.tar.gz
sha256sum "$archive"
snapshot_dir="$(mktemp -d "$(dirname -- "$archive")/lrm-eval-release.XXXXXXXX")"
tar -xzf "$archive" -C "$snapshot_dir"
(
  cd "$snapshot_dir"
  export LC_ALL=C
  find . -type f -print0 | sort -z | xargs -0 -r sha256sum --zero | sha256sum
)
```

校验匹配后，保留解包目录作为发布输入。将 `LRM_EVAL_ROOT` 指向该目录后运行其中的 `backend/scripts/start_https_demo.sh --start`，即可使用包内已构建的前端。

## 备份、日志与磁盘预算

- 使用 `"$LRM_RUNTIME_DIR/venv/bin/python" "$LRM_RUNTIME_DIR/releases/current/backend/scripts/backup_db.py"` 创建 SQLite 一致性在线备份。请在备份前检查临时快照与压缩文件所需空间，并串行执行。脚本在 `state/backups/` 原子写出 `0600` 的 `.db.zst`，本地仅保留最新一份。恢复时先停 Web 与 Runner，将备份解到独立路径并执行 SQLite `PRAGMA integrity_check;`，归档原数据库及其 WAL/SHM，再将校验通过的备份安装到原数据库路径，最后重启验收。
- `state/logs/` 使用应用内轮转，Web 和 HTTPS 代理各保留 4 个 15 MiB 文件，Runner 保留 2 个，服务日志总额 150 MiB。作业 stdio 每个最多 2 MiB，全体目标上限 20 MiB，优先清理已结束作业的旧日志。详情 API 最多读取尾部 128 KiB 并返回最多 200 行脱敏日志。
- `/admin/health` 达到 600 MB 即告警，本地硬上限为 750 MB。预算涵盖 Python 环境、当前与上一版发布、SQLite 和 WAL/SHM、单份备份、常驻及临时日志、密钥和本地工作数据。发布与备份前使用 `du -sh "$LRM_RUNTIME_DIR"`、`df -h "$LRM_RUNTIME_DIR"` 检查峰值余量。遇到 `LOCAL_DISK_LOW` 时按上文保护契约处理，释放可清理空间或扩容后再恢复写入。
