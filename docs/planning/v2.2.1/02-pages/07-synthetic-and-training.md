# 02-7 · 合成题工坊、RL/SFT 四通道选题与训练/OPD 监控

> **关联元数据**
> - **真实数据源**：`/home/share-data/tianxi_benches/datasets/tianxi-synth-v1/` & `datasets/synthetic/`（`synthesis.json`, `harbor.toml`）+ `harbor-runs/*/selection.json` & `episodes.jsonl`（如 `round1` 40×8 = 320 trials）+ `/home/share-data/tianxi_benches/training-runs/`（`routing.json`, `checkpoint-XXXX/opd.json`）+ `framework/{synthesize.py, train.py, training_opd.py}`
> - **核心 API**：`GET /api/v1/synth/datasets`、`POST /api/v1/synth/generate`、`POST /api/v1/synth/check`、`GET /api/v1/synth/selection/{batch_id}`、`POST /api/v1/synth/selection/{batch_id}/preview`、`GET /api/v1/training/runs`、`GET /api/v1/training/runs/{run_id}`
> - **权限要求**：查看列表与汇总 `Viewer+`；留出题 `holdout/` 内容需 `dataset:holdout_read`；触发生成/验收/导出训练清单 `Researcher+`
> - **交付里程碑**：Synthetic Studio 与 RL/SFT 选题看板在 **M2** 交付；Training & OPD 监控在 **M3** 交付

---

## 1. Synthetic Studio 合成题工坊（`/synth/datasets`）

### 1.1 已有合成题包审阅
- 扫描 `datasets/tianxi-synth-v1/` 与 `datasets/synthetic/*`，读取每个题包的 `synthesis.json` 与 `harbor.toml`：
  - 展示版本名、随机种子 `seed`、覆盖方法（`order-reconcile` / `log-rollup` / `toolspace` / `self-questioning` / `multi-hop`）、训练题数 vs 留出题数、`teacher_model`（如 `deepseek-v4.1-flash`）；
  - 审阅教师简报卡片（`teacher_notes` 中的 `scenario`、`edge_cases`、`demonstration`），帮助算法同事快速质检题面质量。
- 点击任意题包可展开训练题列表并跳转题目浏览器（留出集受 `dataset:holdout_read` 保护）。

### 1.2 可视化生成与本地验收（完整对接 `synthesize.py`）
- **生成表单（`synthesize.py generate`）**：配置版本名、勾选 1–5 种 `--method`、`--train-per-method`、`--holdout-per-method`、`--seed`、是否启用 `--teacher`（仅选服务端受保护教师 Profile）及 `toolspace.graph_file`；支持生成 CLI 命令或提交受控后台任务。
- **验收面板（`synthesize.py check`）**：针对任意合成题包运行确定性验收，可视化返回 JSON 报告：`tested`、`passed`、`independent_audit`（独立审计重算通过数）、以及逐题失败原因明细（参考解 1 分 / 独立正确产物 1 分 / 空跑 0 分 / 典型错误 0 分 / 评分器异常非零退出）。

---

## 2. RL / SFT 选题分流与统计稳定性看板（`/synth/selection`）

专为 `round1`（40 题 × 8 尝试 = 320 trials）及未来所有 `-train` 候选轮次打造：

1. **顶部 4 通道漏斗与门槛仪表盘（基于 `selection.json`）**：
   - `GRPO 通道`（混合成败 `0 < passed < attempts`，核心在线强化学习题池）+ `threshold_met` 门槛灯（对比 `--min-grpo`）；
   - `Teacher SFT 通道`（`0/8` 全错，交由强教师在 Harbor 实跑采集 SFT 轨迹）；
   - `Regression 通道`（`8/8` 全对，留作回归测试或提示加难）；
   - `Review 通道`（含运行异常/超时/checksum 漂移/样本不完整，需人工复核）。
   - **动态阈值模拟滑块**：在页面实时调整 `--min-grpo` 或模拟排除指定异常类型，调用 `POST .../selection/{batch_id}/preview` 即时重算分流（不改原文件），并支持一键导出各通道 `task_id` 列表（JSON / TXT）。
2. **多次采样统计置信度与 `pass@k` / `pass^k` 曲线**：
   - **`pass@1..k` 上限曲线**：展示 $k=1 \dots 8$ 的无偏估计曲线与 95% Wilson/Bootstrap 置信区间误差带；
   - **`pass^k` 稳定性曲线**：展示连续 $k$ 次全部通过的稳定性衰减曲线；
   - 严守 `k > n_valid_scored` 缺测规范：顶部醒目标明每个 $k$ 对应的有效可算题数分母（197 实测 `round1` 中 21 题含失败尝试，`pass@8` 仅 **19/40** 题有效可算），并提供切换查看「故障按 0 计入的保守下界曲线」。
3. **40×8 逐题采样热力点阵表**：每行一题，8 列对应 8 次尝试状态圆点 + `excluded` 异常统计 + 所属分流通道，点击任一圆点直达对应 Trial 显微镜。

---

## 3. Training & 多专家 OPD 监控（`/training/opd`）

> **当前 197 实况透明声明**：截至 2026-11-24，197 共享盘 `/home/share-data/tianxi_benches/training-runs/` 目录**当前为空**（0 次实跑产物），但其数据输出契约已在 `framework/training_opd.py`（`routing.json` + `checkpoint-XXXX/opd.json`）与 `framework/train.py` 源码中完整定型。

### 3.1 真实源码产出契约视图
1. **多专家路由拓扑图（读取 `routing.json`）**：以桑基图/拓扑图展示 `Dataset / Environment / Source / Task → Expert Name → Expert Model` 的路由分布与规则优先级（`priority` 与匹配字段数）。
2. **步级训练指标双轴趋势图（读取 `checkpoint-XXXX/opd.json`）**：横轴 `Step 0 → N`，双纵轴同时绘制 **`sampled_reverse_kl`** 下降曲线与 **`Harbor reward`** 爬升曲线，按 Expert 分色拆解并展示 `rollout_checkpoint` 与 `tokens`。
3. **训练前后留出集对照联动（严格护栏）**：仅当 `checkpoint-XXXX` 与对应基线模型均拥有**明确 `checkpoint` 身份、相同留出题集（`-holdout`）且相同评分协议**时，生成「Base → Checkpoint Step N」留出集增益对照卡。

### 3.2 空态表现、契约测试边界与真实运行验证前提
- **生产空态表现**：当 `training-runs/` 为空时，页面呈现清晰空态引导卡 + `train.py --plan` / `training_opd.py` CLI 预检命令生成器，**绝不将测试夹具数据注入生产数据库或混入任何线上榜单/图表**。
- **契约测试夹具边界**：开发与 CI 自动化测试使用严格按 `training_opd.py` 源码输出 Schema 构造的隔离夹具（`tests/fixtures/opd_sample/`，含 `routing.json` + `checkpoint-0000`～`checkpoint-0005/opd.json`），**仅用于契约解析单测与前端组件渲染测试**。
- **验证 1 份真实运行结果所需前置条件**：
  1. 在带 GPU 的训练机上基于 1 个已通过 `report.py --check-training-captures` 的 `harbor-runs/` 采集批次，运行 1 次最小步数（如 `1–2 steps`）的 `training_opd.py`；
  2. 将产出的 `routing.json` 与至少 1 个 `checkpoint-XXXX/opd.json` 发布到 `/home/share-data/tianxi_benches/training-runs/<run_id>/`；
  3. 197 平台热层索引器自动发现该目录，前端正常渲染真实路由拓扑与步级指标（无需重启服务）。
