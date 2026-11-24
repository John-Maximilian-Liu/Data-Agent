"""Authorization — roles, scoped bindings, effective access."""

from dataagent.authorization.policy import (
    AuthContext,
    EffectiveAccess,
    authorize,
    bump_policy_revision,
    evaluate_effective_access,
    require_action,
    require_fields,
)
from dataagent.authorization.roles import SYSTEM_ROLES, get_system_role

__all__ = [
    "AuthContext",
    "EffectiveAccess",
    "SYSTEM_ROLES",
    "authorize",
    "bump_policy_revision",
    "evaluate_effective_access",
    "get_system_role",
    "require_action",
    "require_fields",
]
