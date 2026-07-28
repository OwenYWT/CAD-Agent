"""Project roles and authorization decisions."""
from __future__ import annotations

from enum import StrEnum


class ProjectRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class Permission(StrEnum):
    VIEW_PROJECT = "view_project"
    MODIFY_DESIGN = "modify_design"
    RUN_VALIDATION = "run_validation"
    EXPORT_ARTIFACT = "export_artifact"
    MANAGE_MEMBERS = "manage_members"
    REVIEW_CHANGE = "review_change"
    COMMIT_VERSION = "commit_version"
    ROLLBACK_VERSION = "rollback_version"
    DELETE_PROJECT = "delete_project"


_ROLE_PERMISSIONS: dict[ProjectRole, frozenset[Permission]] = {
    ProjectRole.OWNER: frozenset(Permission),
    ProjectRole.ADMIN: frozenset(
        {
            Permission.VIEW_PROJECT,
            Permission.MODIFY_DESIGN,
            Permission.RUN_VALIDATION,
            Permission.EXPORT_ARTIFACT,
            Permission.MANAGE_MEMBERS,
            Permission.REVIEW_CHANGE,
            Permission.COMMIT_VERSION,
            Permission.ROLLBACK_VERSION,
        }
    ),
    ProjectRole.EDITOR: frozenset(
        {
            Permission.VIEW_PROJECT,
            Permission.MODIFY_DESIGN,
            Permission.RUN_VALIDATION,
            Permission.EXPORT_ARTIFACT,
            Permission.REVIEW_CHANGE,
        }
    ),
    ProjectRole.VIEWER: frozenset({Permission.VIEW_PROJECT}),
}


def role_allows(role: ProjectRole | str, permission: Permission | str) -> bool:
    return Permission(permission) in _ROLE_PERMISSIONS[ProjectRole(role)]
