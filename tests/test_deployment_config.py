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
