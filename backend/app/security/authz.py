from __future__ import annotations

from app.api.errors import AppError
from app.security.auth import AuthUser

ALL_SCOPES: tuple[str, ...] = (
    "trace:read",
    "gateway:read",
    "verifier_log:read",
    "dataset:answer_read",
    "dataset:holdout_read",
    "direct_vlm_qa:read",
)

ROLE_RANK: dict[str, int] = {
    "viewer": 1,
    "researcher": 2,
    "admin": 3,
}


def is_personal_project(project_id: str) -> bool:
    return project_id.startswith("personal:")


def personal_project_id(user_id: str) -> str:
    return f"personal:{user_id}"


def can_access_project(user: AuthUser, project_id: str, owner_id: str | None = None) -> bool:
    """Strict project boundary check (v2.2.1 06-1 §2.2 & S-02):
    - Admin bypasses project restriction.
    - Personal project (`personal:<uid>`): only accessible to its owner (and admin).
    - Team project (non-`personal:*`): ONLY accessible if `project_id in user.projects`.
      Crucially, `owner_id == user.user_id` MUST NOT bypass team project membership revocation!
    """
    if user.role == "admin":
        return True
    if is_personal_project(project_id):
        expected = personal_project_id(user.user_id)
        if project_id != expected:
            return False
        if owner_id is not None and owner_id != user.user_id:
            return False
        return True
    # Team project: strictly membership-based, NO owner_id bypass
    return project_id in user.projects


def authorized_projects_for_user(user: AuthUser) -> list[str] | None:
    """Return allowed project_ids for SQL WHERE filtering BEFORE dedup,
    or None if user is admin (all projects allowed)."""
    if user.role == "admin":
        return None
    projs = set(user.projects)
    # Remove any foreign personal:* projects defensively
    projs = {p for p in projs if not is_personal_project(p)}
    projs.add(personal_project_id(user.user_id))
    return sorted(projs)


def sql_project_filter(user: AuthUser, column: str = "project_id") -> tuple[str, list[str]]:
    """Build parameterized SQL WHERE clause enforcing project_id boundary (S-02)."""
    projs = authorized_projects_for_user(user)
    if projs is None:
        return "1=1", []
    if not projs:
        return "1=0", []
    placeholders = ",".join("?" for _ in projs)
    return f"{column} IN ({placeholders})", list(projs)


def sql_task_holdout_exclude_clause(release_expr: str, task_id_expr: str) -> str:
    """Reusable SQL fragment hiding per-task holdout entries in mixed datasets (`task_definitions.split = 'holdout'`)."""
    return (
        f"NOT EXISTS ("
        f"SELECT 1 FROM task_definitions _td_ho "
        f"WHERE _td_ho.dataset_release_id = {release_expr} "
        f"AND _td_ho.task_id = {task_id_expr} "
        f"AND LOWER(COALESCE(_td_ho.split, '')) = 'holdout'"
        f")"
    )


def sql_holdout_exclude_clause(
    batch_alias: str = "b",
    release_alias: str = "r",
    *,
    task_id_expr: str | None = None,
    release_expr: str | None = None,
) -> str:
    """Reusable SQL fragment hiding whole-release/batch AND per-task holdout details when caller lacks dataset:holdout_read."""
    base = (
        f"LOWER(COALESCE({release_alias}.split, '')) != 'holdout' "
        f"AND LOWER(COALESCE({batch_alias}.dataset_release_id, '')) NOT LIKE '%holdout%' "
        f"AND LOWER(COALESCE({batch_alias}.raw_dataset_name, '')) NOT LIKE '%holdout%' "
        f"AND LOWER(COALESCE({batch_alias}.batch_name, '')) NOT LIKE '%holdout%'"
    )
    if task_id_expr:
        rel_e = release_expr or f"{batch_alias}.dataset_release_id"
        return f"{base} AND {sql_task_holdout_exclude_clause(rel_e, task_id_expr)}"
    return base


def require_role(user: AuthUser, min_role: str) -> None:
    if ROLE_RANK.get(user.role, 0) < ROLE_RANK.get(min_role, 99):
        raise AppError(
            403,
            "SCOPE_FORBIDDEN",
            f"需要 {min_role} 及以上角色权限",
            {"required_role": min_role, "current_role": user.role},
        )


def require_project_access(user: AuthUser, project_id: str, owner_id: str | None = None) -> None:
    if not can_access_project(user, project_id, owner_id=owner_id):
        raise AppError(
            403,
            "PROJECT_FORBIDDEN",
            "无该项目访问权限或成员资格已撤销",
            {"project_id": project_id},
        )


def has_scope(user: AuthUser, scope: str) -> bool:
    return scope in user.scopes


def require_scope(user: AuthUser, scope: str) -> None:
    if not has_scope(user, scope):
        raise AppError(
            403,
            "SCOPE_FORBIDDEN",
            f"缺少 {scope} 权限，禁止访问受限明细",
            {"missing_scope": scope},
        )


def authorize(
    user: AuthUser,
    *,
    project_id: str,
    owner_id: str | None = None,
    min_role: str = "viewer",
    required_scope: str | None = None,
) -> None:
    require_role(user, min_role)
    require_project_access(user, project_id, owner_id=owner_id)
    if required_scope:
        require_scope(user, required_scope)


def is_holdout_dataset_or_batch(
    *,
    dataset_release_id: str | None = None,
    release_split: str | None = None,
    benchmark_slug: str | None = None,
    benchmark_dir: str | None = None,
    raw_dataset_name: str | None = None,
    batch_name: str | None = None,
) -> bool:
    """Unified holdout split predicate reused across Datasets, Batches, Profiles, and Compare (S-02)."""
    if release_split and str(release_split).strip().lower() == "holdout":
        return True
    for val in (dataset_release_id, benchmark_slug, benchmark_dir, raw_dataset_name, batch_name):
        if val and "holdout" in str(val).lower():
            return True
    return False
