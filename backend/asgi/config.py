"""Environment-backed server configuration."""
from dataclasses import dataclass
import os
from pathlib import Path


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    version: str = "0.1.0"
    host: str = os.environ.get("HLHQ_HOST", "0.0.0.0")
    port: int = int(os.environ.get("HLHQ_PORT", "8770"))
    icon_http_port: int = int(os.environ.get("HLHQ_ICON_HTTP_PORT", "8771"))
    web_dir: Path = Path(os.environ.get(
        "HLHQ_WEB_DIR", str(Path(__file__).resolve().parents[2] / "web")
    )).resolve()
    max_json_body_bytes: int = max(
        1, int(os.environ.get("HLHQ_MAX_JSON_BODY_BYTES", "1048576"))
    )
    http_request_timeout: int = max(
        1, int(os.environ.get("HLHQ_HTTP_REQUEST_TIMEOUT", "30"))
    )
    trust_proxy: bool = _flag("HLHQ_TRUST_PROXY")
    external_https: bool = _flag("HLHQ_EXTERNAL_HTTPS")
    readiness_timeout_seconds: float = max(
        0.1, float(os.environ.get("HLHQ_READINESS_TIMEOUT", "2"))
    )

    @property
    def tls_requested(self) -> bool:
        if os.environ.get("HLHQ_TLS_CERT") and os.environ.get("HLHQ_TLS_KEY"):
            return True
        return os.environ.get("HLHQ_TLS", "").strip().lower() in {
            "1", "true", "yes", "on", "auto",
        }


settings = Settings()
