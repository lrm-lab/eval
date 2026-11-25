from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _default_runtime_dir() -> Path:
    return Path(os.environ.get("LRM_RUNTIME_DIR") or (Path.home() / ".local" / "share" / "lrm-eval"))


@dataclass
class Settings:
    eval_root: Path = field(default_factory=lambda: Path(os.environ.get("LRM_EVAL_ROOT", "/home/share-data/eval")))
    tianxi_root: Path = field(default_factory=lambda: Path(os.environ.get("LRM_TIANXI_ROOT", "/home/share-data/tianxi_benches")))
    dataset_root: Path = field(default_factory=lambda: Path(os.environ.get("LRM_DATASET_ROOT", "/home/share-data/dataset")))
    config_dir: Path = field(default_factory=lambda: Path(os.environ.get("LRM_CONFIG_DIR", "/home/share-data/eval/config")))
    managed_framework_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "LRM_MANAGED_FRAMEWORK_DIR",
                str(Path(__file__).resolve().parent / "managed_framework"),
            )
        )
    )

    runtime_dir: Path = field(default_factory=_default_runtime_dir)
    state_dir: Path | None = None
    staging_dir: Path | None = None
    evidence_store_dir: Path | None = None
    runner_work_dir: Path | None = None
    published_batches_dir: Path | None = None
    db_path: Path | None = None
    secrets_file: Path | None = None
    gateways_env_file: Path | None = None
    frontend_dist_dir: Path = field(default_factory=lambda: Path(os.environ.get("LRM_FRONTEND_DIST_DIR", "/home/share-data/eval/frontend/dist")))

    def __post_init__(self) -> None:
        if self.state_dir is None:
            self.state_dir = Path(os.environ.get("LRM_STATE_DIR") or (self.runtime_dir / "state"))
        if self.staging_dir is None:
            self.staging_dir = Path(os.environ.get("LRM_STAGING_DIR") or (self.runtime_dir / "uploads-staging"))
        if self.evidence_store_dir is None:
            self.evidence_store_dir = Path(os.environ.get("LRM_EVIDENCE_STORE_DIR") or (self.runtime_dir / "evidence-store"))
        if self.runner_work_dir is None:
            self.runner_work_dir = Path(os.environ.get("LRM_RUNNER_WORK_DIR") or (self.runtime_dir / "runner-work"))
        if self.published_batches_dir is None:
            self.published_batches_dir = Path(os.environ.get("LRM_PUBLISHED_BATCHES_DIR") or (self.runtime_dir / "published-dto"))
        if self.db_path is None:
            self.db_path = Path(os.environ.get("LRM_DB_PATH") or (self.state_dir / "eval_platform.db"))
        if self.secrets_file is None:
            self.secrets_file = Path(os.environ.get("LRM_SECRETS_FILE") or (self.state_dir / "secrets.env"))
        if self.gateways_env_file is None:
            self.gateways_env_file = Path(os.environ.get("LRM_GATEWAYS_ENV_FILE") or (self.state_dir / "gateways.env"))

    # Security & HTTP
    production_mode: bool = field(default_factory=lambda: _env_bool("LRM_PRODUCTION_MODE", False))
    cookie_secure: bool = field(default_factory=lambda: _env_bool("COOKIE_SECURE", True))
    session_ttl_seconds: int = field(default_factory=lambda: int(os.environ.get("LRM_SESSION_TTL", "86400")))
    allowed_origins: list[str] = field(default_factory=lambda: [
        o.strip()
        for o in os.environ.get(
            "LRM_ALLOWED_ORIGINS",
            "http://127.0.0.1:8680,http://localhost:8680,http://10.110.158.197:8680,https://10.110.158.197:8443,http://testserver",
        ).split(",")
        if o.strip()
    ])
    secret_key: str | None = field(default_factory=lambda: os.environ.get("LRM_EVAL_SECRET_KEY"))
    methodology_version: str = "v2026.11.24"

    # Indexing & Budget
    poll_interval_seconds: int = field(default_factory=lambda: int(os.environ.get("LRM_POLL_INTERVAL_SECONDS", "15")))
    enable_background_indexer: bool = field(default_factory=lambda: _env_bool("LRM_ENABLE_BG_INDEXER", True))
    local_budget_mb: int = 750
    root_disk_min_free_gb: float = 1.5
    max_upload_bytes: int = 50 * 1024 * 1024

    # External Isolated Execution Runner IPC (M3 — Web NEVER executes fake in-process workers!)
    runner_ipc_socket: Path | None = field(
        default_factory=lambda: Path(os.environ["LRM_RUNNER_SOCKET_PATH"]) if os.environ.get("LRM_RUNNER_SOCKET_PATH") else None
    )
    runner_ipc_url: str | None = field(
        default_factory=lambda: (os.environ.get("LRM_RUNNER_IPC_URL") or "").strip() or None
    )
    runner_ipc_token_file: Path | None = field(
        default_factory=lambda: Path(os.environ["LRM_RUNNER_IPC_TOKEN_FILE"]) if os.environ.get("LRM_RUNNER_IPC_TOKEN_FILE") else None
    )
    allow_harbor_runs_publish: bool = field(
        default_factory=lambda: _env_bool("LRM_ALLOW_HARBOR_RUNS_PUBLISH", False)
    )

    @property
    def harbor_runs_dir(self) -> Path:
        return self.tianxi_root / "harbor-runs"

    @property
    def legacy_root_dir(self) -> Path:
        return self.tianxi_root / "2026-11-20"

    @property
    def datasets_dir(self) -> Path:
        return self.tianxi_root / "datasets"

    @property
    def bundles_dir(self) -> Path:
        return self.tianxi_root / "bundles"

    @property
    def training_runs_dir(self) -> Path:
        return self.tianxi_root / "training-runs"

    @property
    def framework_dir(self) -> Path:
        return self.tianxi_root / "framework"

    @property
    def taxonomy_file(self) -> Path:
        return self.config_dir / "taxonomy.yaml"

    @property
    def media_rules_file(self) -> Path:
        return self.config_dir / "media_matching_rules.yaml"

    @property
    def source_approval_file(self) -> Path:
        return self.config_dir / "source_approval_registry.yaml"

    @property
    def token_semantics_file(self) -> Path:
        return self.config_dir / "token_semantics_registry.yaml"

    @property
    def gateway_profiles_file(self) -> Path:
        return self.config_dir / "gateway_profiles.yaml"

    @property
    def direct_scorers_file(self) -> Path:
        return self.config_dir / "direct_scorers.yaml"

    @property
    def direct_suites_file(self) -> Path:
        return self.config_dir / "direct_suites.yaml"
