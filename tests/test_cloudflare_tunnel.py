from pathlib import Path

import yaml
import pytest


ROOT = Path(__file__).parents[1]
COMPOSE_PATH = ROOT / "compose.yaml"
DOC_PATH = ROOT / "docs" / "CLOUDFLARE_TUNNEL.md"
TOKEN_FILE = "./secrets/cloudflare_tunnel_token.txt"
TOKEN_TARGET = "/run/secrets/cloudflare_tunnel_token"

pytestmark = pytest.mark.skipif(
    not COMPOSE_PATH.exists(),
    reason="repository deployment files are not included in the runtime image",
)


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def test_cloudflared_is_an_optional_independent_service() -> None:
    services = _compose()["services"]
    tunnel = services["cloudflared"]

    assert tunnel["profiles"] == ["tunnel"]
    assert tunnel["image"] == "cloudflare/cloudflared:2026.8.2"
    assert "build" not in tunnel
    assert tunnel["depends_on"] == {
        "geoflow": {"condition": "service_healthy"}
    }


def test_cloudflared_uses_a_file_secret_and_is_hardened() -> None:
    compose = _compose()
    tunnel = compose["services"]["cloudflared"]

    assert tunnel["command"] == [
        "tunnel",
        "--no-autoupdate",
        "run",
        "--token-file",
        TOKEN_TARGET,
    ]
    assert tunnel["secrets"] == ["cloudflare_tunnel_token"]
    assert compose["secrets"]["cloudflare_tunnel_token"]["file"] == TOKEN_FILE
    assert "environment" not in tunnel
    assert tunnel["read_only"] is True
    assert tunnel["cap_drop"] == ["ALL"]
    assert tunnel["security_opt"] == ["no-new-privileges:true"]


def test_cloudflared_shares_only_the_application_network() -> None:
    services = _compose()["services"]
    geoflow = services["geoflow"]
    tunnel = services["cloudflared"]

    assert set(geoflow["networks"]) & set(tunnel["networks"])
    assert "volumes" not in tunnel
    serialized = str(tunnel)
    for forbidden in ("/data", "ODA", "docker.sock", ".dwg"):
        assert forbidden not in serialized


def test_tunnel_secret_is_excluded_from_git_and_docker_context() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    ignored_path = "secrets/cloudflare_tunnel_token.txt"

    assert ignored_path in gitignore
    assert ignored_path in dockerignore


def test_documented_origin_uses_the_docker_service_name() -> None:
    documentation = DOC_PATH.read_text(encoding="utf-8")

    assert "http://geoflow:8000" in documentation
    assert "docker compose --profile tunnel up -d --build" in documentation
