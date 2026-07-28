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
        }
    ),
    ProjectRole.EDITOR: frozenset(
        {
            Permission.VIEW_PROJECT,
            Permission.MODIFY_DESIGN,
            Permission.RUN_VALIDATION,
            Permission.EXPORT_ARTIFACT,
        }
    ),
    ProjectRole.VIEWER: frozenset({Permission.VIEW_PROJECT}),
}


def role_allows(role: ProjectRole | str, permission: Permission | str) -> bool:
    return Permission(permission) in _ROLE_PERMISSIONS[ProjectRole(role)]
