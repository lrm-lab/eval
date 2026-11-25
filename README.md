# LRM Eval 前端展示与分析平台

基于 `lrm-lab/lrm-eval` 与 197（`LRM-02`）全量评测数据源构建的统一评测展示、对比与细粒度诊断平台。

## 权威设计文档
- [v2.2.1 终审规划文档](docs/planning/v2.2.1/README.md)

## 目录结构（三区存储隔离）
- `/home/share-data/eval/`（CIFS 共享盘，22T）：协作源码、`config/` 配置、`docs/planning/` 规划文档与构建缓存。
- `${LRM_RUNTIME_DIR:-$HOME/.local/share/lrm-eval}`（197 本地 ext4 `0700` 独占目录，严守 ≤ 750 MB 预算）：
  - `bin/uv` 与精简 Python 3.12 `.venv`
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

## 快速启动与验收（M0–M1）
```bash
# 1. 初始化精简 Python 环境（利用 /dev/shm 加速并保持根盘零缓存残留）
bash backend/scripts/bootstrap_env.sh

# 2. 在 /dev/shm tmpfs 中构建前端产物 frontend/dist（CIFS 零软链、根盘零缓存）
bash backend/scripts/build_frontend.sh

# 3. 部署本地受管发布副本（含 backend + config + frontend/dist，生产模式禁止从 CIFS 加载）
bash backend/scripts/deploy_release.sh

# 4. 通过环境变量或交互式 CLI 初始化管理员账号（零硬编码默认密码）
LRM_ADMIN_PASSWORD='<strong-password>' "${HOME}/.local/share/lrm-eval/venv/bin/python" backend/scripts/init_admin.py --username admin --display-name "Admin"

# 5. 运行全量 M0+M1 自动化测试（含 197 实机黄金样本、三线护栏裁决、效率前沿、低水位写保护与安全零旁路验收）
cd /home/share-data/eval/backend && "${HOME}/.local/share/lrm-eval/venv/bin/pytest" tests --basetemp=/dev/shm/pytest-lrm-eval -v

# 6. 启动服务（默认 LRM_PRODUCTION_MODE=true，从 releases/current 启动，监听 127.0.0.1:8680）及用户态反代
bash backend/scripts/run_server.sh
# 可选反代（197 无系统 nginx/caddy 时）：
# "${HOME}/.local/share/lrm-eval/venv/bin/python" backend/scripts/reverse_proxy.py --listen-port 8080 --upstream http://127.0.0.1:8680
```
