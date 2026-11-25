from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.errors import AppError, app_error_handler
from app.api.routes_auth import router as auth_router
from app.api.routes_batches import router as batches_router
from app.api.routes_compare import router as compare_router
from app.api.routes_datasets import router as datasets_router
from app.api.routes_diagnostics import router as diagnostics_router
from app.api.routes_direct_lab import router as direct_lab_router
from app.api.routes_execution import router as execution_router
from app.api.routes_leaderboard import router as leaderboard_router
from app.api.routes_profiles import router as profiles_router
from app.api.routes_public_baselines import router as public_baselines_router
from app.api.routes_synth import router as synth_router
from app.api.routes_system import router as system_router
from app.api.routes_toolkit import router as toolkit_router
from app.api.routes_training import router as training_router
from app.config import Settings
from app.db.database import Database
from app.db.migrations import init_db, sync_taxonomy_to_db
from app.indexing.coordinator import IndexCoordinator
from app.security.auth import SessionManager, load_or_create_secret_key
from app.security.gateways import GatewayProfileRegistry
from app.security.managed_exec import verify_managed_framework_bundle
from app.security.storage_probe import (
    UNSAFE_FSTYPES,
    check_disk_write_guard,
    get_mount_fstype,
    probe_isolated_dir,
    probe_managed_release_path,
    probe_secret_file,
)
from app.services.compare_service import CompareService
from app.services.diagnostics_service import DiagnosticsService
from app.services.direct_lab_service import DirectLabService
from app.services.execution_service import ExecutionService
from app.services.profile_service import ProfileService
from app.services.public_baseline_service import PublicBaselineService
from app.services.query_service import QueryService
from app.services.synth_service import SynthService
from app.services.toolkit_service import ToolkitService
from app.services.training_service import TrainingService


@dataclass
class AppContext:
    settings: Settings
    db: Database
    sessions: SessionManager
    coordinator: IndexCoordinator
    query: QueryService
    compare: CompareService
    profiles: ProfileService
    public_baselines: PublicBaselineService
    gateway_profiles: GatewayProfileRegistry
    diagnostics: DiagnosticsService
    synth: SynthService
    direct_lab: DirectLabService
    toolkit: ToolkitService
    execution: ExecutionService
    training: TrainingService
    write_guard: Callable[[], None]


def _verify_all_runtime_isolation(
    cfg: Settings,
    *,
    guard_for_path: Callable[[Path], None] | None = None,
) -> None:
    """Fail-Closed POSIX 0700 / 0600 probes covering ALL actual runtime locations:
    state_dir, db_path.parent, secrets_file (+parent), staging_dir, evidence_store_dir, runner_work_dir,
    plus managed config_dir & code directory when production_mode=True."""
    assert cfg.state_dir is not None and cfg.db_path is not None and cfg.secrets_file is not None
    assert cfg.staging_dir is not None and cfg.evidence_store_dir is not None and cfg.runner_work_dir is not None

    seen: set[Path] = set()
    for p in (
        cfg.state_dir,
        cfg.db_path.parent,
        cfg.staging_dir,
        cfg.evidence_store_dir,
        cfg.runner_work_dir,
    ):
        rp = p.resolve() if p.exists() else p
        if rp not in seen:
            wg = (lambda target=p: guard_for_path(target)) if guard_for_path is not None else None
            probe_isolated_dir(p, create=True, write_guard=wg)
            seen.add(rp)

    if cfg.db_path.exists() or cfg.db_path.is_symlink():
        db_fs = get_mount_fstype(cfg.db_path)
        if db_fs in UNSAFE_FSTYPES:
            raise AppError(
                503,
                "STAGING_PERMISSION_UNSAFE",
                f"数据库文件严禁落在不支持 POSIX 权限的文件系统 ({db_fs}) 上",
                {"fstype": db_fs},
            )

    sec_wg = (lambda: guard_for_path(cfg.secrets_file.parent)) if guard_for_path is not None else None  # type: ignore[union-attr]
    probe_secret_file(cfg.secrets_file, create_parent=True, write_guard=sec_wg)

    if cfg.production_mode:
        # Production deployment MUST load managed code, approval YAML & pinned framework scripts from local POSIX FS!
        probe_managed_release_path(cfg.config_dir, label="config_dir", require_strict_0600=False)
        probe_managed_release_path(
            cfg.source_approval_file, label="source_approval_registry", require_strict_0600=True
        )
        probe_managed_release_path(Path(__file__).resolve().parent, label="app_code_dir", require_strict_0600=False)
        verify_managed_framework_bundle(cfg, require_posix_strict=True)


def create_app(
    settings: Settings | None = None,
    *,
    run_startup_scan: bool = True,
    disk_usage_fn=None,
) -> FastAPI:
    cfg = settings or Settings()
    assert cfg.db_path is not None and cfg.secrets_file is not None

    def _guard_for_path(target_dir: Path) -> None:
        kwargs = {
            "min_free_gb": cfg.root_disk_min_free_gb,
            "runtime_dir": cfg.runtime_dir,
            "local_budget_mb": cfg.local_budget_mb,
        }
        if disk_usage_fn is not None:
            kwargs["disk_usage_fn"] = disk_usage_fn
        check_disk_write_guard(target_dir, **kwargs)  # type: ignore[arg-type]

    _verify_all_runtime_isolation(cfg, guard_for_path=_guard_for_path)

    def _disk_write_guard() -> None:
        _guard_for_path(cfg.db_path.parent)  # type: ignore[union-attr]

    def _secrets_write_guard() -> None:
        _guard_for_path(cfg.secrets_file.parent)  # type: ignore[union-attr]

    # Guard ALL secret file creations, incomplete-file rewrites, and rotations on secrets_file's ACTUAL mount!
    secret_key = load_or_create_secret_key(
        cfg.secrets_file,
        cfg.secret_key,
        write_guard=_secrets_write_guard,
    )

    db = Database(cfg.db_path, write_guard=_disk_write_guard)
    startup_write_protected = False
    startup_block_reason: str | None = None

    if cfg.db_path.is_file():
        # If DB already exists and disk is low, enter strict read-only write-protected mode
        # (ZERO -wal / -shm sidecar creation on subsequent reads)!
        try:
            init_db(db)
            sync_taxonomy_to_db(db, cfg.taxonomy_file)
        except AppError as exc:
            if exc.code == "LOCAL_DISK_LOW":
                db.set_read_only_mode(True)
                startup_write_protected = True
                startup_block_reason = exc.message
            else:
                raise
    else:
        # Brand new DB cannot be created when disk is below protection line -> fail closed 503 LOCAL_DISK_LOW
        init_db(db)
        sync_taxonomy_to_db(db, cfg.taxonomy_file)

    coordinator = IndexCoordinator(cfg, db)
    if startup_write_protected:
        coordinator.write_protected = True
        coordinator.write_block_reason = startup_block_reason

    query_svc = QueryService(cfg, db)
    gateway_reg = GatewayProfileRegistry(cfg)
    ctx = AppContext(
        settings=cfg,
        db=db,
        sessions=SessionManager(secret_key, ttl_seconds=cfg.session_ttl_seconds),
        coordinator=coordinator,
        query=query_svc,
        compare=CompareService(cfg, db, approval_registry=query_svc.approval_registry),
        profiles=ProfileService(cfg, db, approval_registry=query_svc.approval_registry),
        public_baselines=PublicBaselineService(db, interval_seconds=cfg.public_baseline_sync_seconds),
        gateway_profiles=gateway_reg,
        diagnostics=DiagnosticsService(cfg, db, approval_registry=query_svc.approval_registry),
        synth=SynthService(cfg, db, approval_registry=query_svc.approval_registry, gateway_profiles=gateway_reg),
        direct_lab=DirectLabService(cfg, db, gateway_profiles=gateway_reg),
        toolkit=ToolkitService(cfg, db, approval_registry=query_svc.approval_registry),
        execution=ExecutionService(
            cfg,
            db,
            coordinator=coordinator,
            gateway_profiles=gateway_reg,
            write_guard=_disk_write_guard,
        ),
        training=TrainingService(cfg, db),
        write_guard=_disk_write_guard,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        ctx.execution.start_recovery()
        if run_startup_scan:
            ctx.coordinator.start()
            ctx.public_baselines.start()
        try:
            yield
        finally:
            ctx.public_baselines.stop()
            ctx.execution.stop()
            ctx.coordinator.stop()
            ctx.db.close_thread_connection()

    app = FastAPI(
        title="LRM Eval Showcase & Analysis Platform",
        version="0.4.0",
        lifespan=lifespan,
    )
    app.state.ctx = ctx
    app.add_exception_handler(AppError, app_error_handler)

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        req_id = f"req_{uuid.uuid4().hex[:12]}"
        request.state.request_id = req_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = req_id
        return response

    app.include_router(auth_router)
    app.include_router(system_router)
    app.include_router(leaderboard_router)
    app.include_router(profiles_router)
    app.include_router(datasets_router)
    app.include_router(batches_router)
    app.include_router(compare_router)
    app.include_router(public_baselines_router)
    app.include_router(diagnostics_router)
    app.include_router(synth_router)
    app.include_router(direct_lab_router)
    app.include_router(toolkit_router)
    app.include_router(execution_router)
    app.include_router(training_router)

    dist_dir = cfg.frontend_dist_dir
    if dist_dir.is_dir():
        assets_dir = dist_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend-assets")

        index_html = dist_dir / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        def serve_spa(full_path: str):
            if full_path.startswith("api/") or full_path == "api":
                raise AppError(404, "RESOURCE_NOT_FOUND", f"API 路由不存在: /{full_path}")
            # Safely serve direct static files inside dist (favicon, manifest, etc.)
            if full_path and ".." not in full_path and not full_path.startswith("/"):
                cand = (dist_dir / full_path).resolve()
                try:
                    cand.relative_to(dist_dir.resolve())
                    if cand.is_file():
                        return FileResponse(str(cand))
                except ValueError:
                    pass
            if index_html.is_file():
                return FileResponse(str(index_html), headers={"Cache-Control": "no-cache"})
            raise AppError(404, "RESOURCE_NOT_FOUND", "前端构建产物未就绪")

    return app


def get_default_app() -> FastAPI:
    return create_app()
