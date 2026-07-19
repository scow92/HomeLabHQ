"""Transport input normalization for route functions."""
from dataclasses import dataclass, field
import json
from urllib.parse import parse_qs, urlparse

from context import Actor
from errors import AuthenticationRequired, ValidationError


def decode_json(handler, max_bytes: int):
    content_type = handler.headers.get("Content-Type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise ValidationError("Content-Type must be application/json")
    raw_length = handler.headers.get("Content-Length")
    if raw_length is None:
        return {}
    if not raw_length.isascii() or not raw_length.isdecimal():
        raise ValidationError("invalid Content-Length")
    length = int(raw_length)
    if length > max_bytes:
        raise ValidationError("JSON body too large")
    if not length:
        return {}
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise ValidationError("incomplete JSON body")
    try:
        body = json.loads(raw)
    except Exception as error:
        raise ValidationError("invalid JSON body") from error
    if not isinstance(body, dict):
        raise ValidationError("JSON body must be an object")
    return body


@dataclass
class Request:
    handler: object
    path: str
    params: dict[str, str] = field(default_factory=dict)
    actor: Actor | None = None
    _body: dict | None = field(default=None, init=False, repr=False)

    @property
    def query(self):
        return parse_qs(urlparse(self.handler.path).query)

    def query_value(self, name, default=None):
        return (self.query.get(name) or [default])[0]

    @property
    def body(self):
        if self._body is None:
            self._body = decode_json(self.handler, self.handler.max_json_body_bytes)
        return self._body

    @property
    def current_user(self):
        return self.handler.current_user()

    def require_actor(self):
        if self.actor is None:
            raise AuthenticationRequired()
        return self.actor
