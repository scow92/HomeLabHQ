"""Declarative, intentionally small HTTP router."""
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Callable


class AuthPolicy(StrEnum):
    PUBLIC = "public"
    AUTHENTICATED = "authenticated"
    ADMIN = "admin"


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    endpoint: Callable
    auth: AuthPolicy = AuthPolicy.AUTHENTICATED
    name: str = ""

    def __post_init__(self):
        if not self.method or self.method.upper() != self.method:
            raise ValueError("route methods must be uppercase")
        if not self.path.startswith("/"):
            raise ValueError("route paths must start with '/'")
        names = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", self.path)
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate path parameter in {self.path}")
        pattern = re.escape(self.path)
        for name in names:
            pattern = pattern.replace(re.escape("{" + name + "}"),
                                      f"(?P<{name}>[^/]+)")
        object.__setattr__(self, "_pattern", re.compile("^" + pattern + "$"))

    def match(self, path: str):
        matched = self._pattern.match(path)
        return matched.groupdict() if matched else None


class Router:
    def __init__(self, routes=()):
        self._routes: list[Route] = []
        self._keys: set[tuple[str, str]] = set()
        self.add_all(routes)

    @property
    def routes(self):
        return tuple(self._routes)

    def add(self, route: Route):
        key = (route.method, route.path)
        if key in self._keys:
            raise ValueError(f"duplicate route: {route.method} {route.path}")
        self._keys.add(key)
        self._routes.append(route)

    def add_all(self, routes):
        for route in routes:
            self.add(route)

    def resolve(self, method: str, path: str):
        for route in self._routes:
            if route.method != method:
                continue
            params = route.match(path)
            if params is not None:
                return route, params
        return None
