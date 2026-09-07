from .auth import Role


def may_call_tool(role: Role, name: str) -> bool:
    return not name.startswith("admin_") or role == "admin"
