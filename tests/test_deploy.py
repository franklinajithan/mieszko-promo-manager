import importlib.util
import socket
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("deploy", Path("scripts/deploy.py"))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "http://192.168.128.139",
        "https://user:pass@example.com",
        "https://example.com/path",
        "https://example.com?x=1",
        "https://example.com\nCOOKIE_SECURE=false",
        "https://example.com:99999",
    ],
)
def test_reject_unsafe_origins(origin):
    with pytest.raises(ValueError):
        deploy.validate_origin(origin)


def test_private_configuration_preserved(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    deploy.configure("https://promo.example.com/", 18085)
    values = deploy.read_config()
    assert values["ALLOWED_ORIGIN"] == "https://promo.example.com"
    assert values["COOKIE_SECURE"] == "true"
    assert values["POSTGRES_PASSWORD"] != values["PROMO_APP_PASSWORD"]
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    text = capsys.readouterr().out
    assert values["POSTGRES_PASSWORD"] not in text
    assert values["PROMO_APP_PASSWORD"] not in text
    before = (tmp_path / ".env").read_bytes()
    with pytest.raises(FileExistsError):
        deploy.configure("https://different.example.com", 18086)
    assert (tmp_path / ".env").read_bytes() == before


def test_tunnel_config_and_symlink_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    outside = tmp_path / "existing-config"
    outside.write_text("untouched")
    (tmp_path / ".env").symlink_to(outside)
    with pytest.raises(FileExistsError):
        deploy.configure("http://127.0.0.1:18085", 18085)
    assert outside.read_text() == "untouched"
    (tmp_path / ".env").unlink()
    deploy.configure("http://127.0.0.1:18085", 18085)
    assert deploy.read_config()["COOKIE_SECURE"] == "false"


def docker_stub(args, **_):
    if "info" in args:
        return "27.0"
    if "version" in args:
        return "2.30"
    return ""


def test_preflight_refuses_occupied_port(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    monkeypatch.setattr(deploy.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(deploy, "run", docker_stub)
    with socket.socket() as existing:
        existing.bind(("127.0.0.1", 0))
        with pytest.raises(ValueError, match="already in use"):
            deploy.preflight(existing.getsockname()[1])


def test_install_refuses_retained_business_volumes(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    monkeypatch.setattr(deploy.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        deploy,
        "run",
        lambda args, **kwargs: "mieszko-promo_postgres_data" if "volume" in args else docker_stub(args),
    )
    with pytest.raises(ValueError, match="data volumes"):
        deploy.preflight(18085)


def test_compose_is_scoped_to_this_project(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy, "run", lambda args, **kwargs: calls.append(args))
    deploy.compose("config", "--quiet")
    command = calls[0]
    assert command[command.index("--project-name") + 1] == "mieszko-promo"
    assert command[command.index("--file") + 1] == str(deploy.ROOT / "docker-compose.yml")
    assert command[command.index("--env-file") + 1] == str(deploy.ROOT / ".env")


def test_compose_does_not_inherit_another_apps_configuration(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "another-app-secret")
    monkeypatch.setenv("COMPOSE_FILE", "/another/application/compose.yml")
    calls = []
    monkeypatch.setattr(deploy, "run", lambda args, **kwargs: calls.append(kwargs))
    deploy.compose("config", "--quiet")
    assert "POSTGRES_PASSWORD" not in calls[0]["env"]
    assert "COMPOSE_FILE" not in calls[0]["env"]


def test_operational_wrapper_ignores_other_project_environment(tmp_path):
    import json
    import os
    import shutil
    import subprocess

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile("scripts/compose.sh", scripts / "compose.sh")
    (tmp_path / ".env").write_text("CONFIGURATION_PROBE=1\n")
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text(
        '#!/usr/bin/env python3\nimport json,os,sys\nprint(json.dumps({"args":sys.argv[1:],"inherited":any(k in os.environ for k in ["COMPOSE_FILE","COMPOSE_PROJECT_NAME","POSTGRES_PASSWORD"])}))\n'
    )
    docker.chmod(0o755)
    env = os.environ | {
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        "COMPOSE_FILE": "/another/app/compose.yml",
        "COMPOSE_PROJECT_NAME": "another-app",
        "POSTGRES_PASSWORD": "another-app-password",
    }
    result = subprocess.run(
        ["bash", str(scripts / "compose.sh"), "config", "--quiet"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(result.stdout)
    assert report["inherited"] is False
    assert report["args"][report["args"].index("--project-name") + 1] == "mieszko-promo"
    assert str(tmp_path / ".env") in report["args"]
    assert "/another/app/compose.yml" not in report["args"]
