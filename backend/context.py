"""Request identity passed into application services.

Keeping the authenticated user as one immutable value prevents the old
``owner_id, is_admin`` argument pairs from drifting apart at call sites.
"""
from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


@dataclass(frozen=True)
class Actor:
    user_id: str
    role: Role

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN

    @classmethod
    def from_user(cls, user: dict) -> "Actor":
        """Build an actor from auth's safe session-user representation."""
        return cls(user_id=user["id"], role=Role(user["role"]))
