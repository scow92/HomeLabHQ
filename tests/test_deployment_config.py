"""Regression coverage for public deployment configuration."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_compose_reads_tls_hosts_from_gitignored_env_file() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "HLHQ_TLS_HOSTS=${HLHQ_TLS_HOSTS:-}" in compose
    assert "HLHQ_TLS_HOSTS=" in example.splitlines()
    assert ".env" in ignored


def test_container_runs_single_worker_uvicorn_and_uses_lightweight_health() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert 'CMD ["python3", "-m", "backend.run"]' in dockerfile
    assert "HLHQ_HOST=0.0.0.0" in compose
    assert "https://127.0.0.1:8770/health" in compose
    assert "workers=1" in (ROOT / "backend" / "run.py").read_text(encoding="utf-8")


def test_container_includes_ping_for_keeplink_monitoring() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "iputils-ping" in dockerfile


def test_data_initializer_publishes_its_oneshot_lifecycle() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "com.homelabhq.lifecycle: oneshot" in compose
