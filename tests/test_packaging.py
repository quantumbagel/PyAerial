"""Packaging: version single-sourcing, sdist contents and deployment files."""

from __future__ import annotations

import pathlib
import re
import tomllib
from importlib.metadata import version

import pyaerial

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_version_comes_from_package_metadata():
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert pyaerial.__version__ == version("pyaerial") == declared


def test_manifest_ships_tests_docs_and_portal_source():
    manifest = (ROOT / "MANIFEST.in").read_text()
    for needed in (
        "recursive-include tests *.py",
        "include *.md",
        "include LICENSE",
        "requirements.lock",
        "docker-compose.yml",
        "graft web",
        "prune web/node_modules",
    ):
        assert needed in manifest, needed
    # Every test support module is a .py under tests/ (covered above).
    for module in ("conftest", "helpers", "fake_redis", "integrity_world", "e2e_support"):
        assert (ROOT / "tests" / f"{module}.py").is_file(), module


def test_license_is_an_spdx_expression():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["license"] == "GPL-3.0-or-later"
    assert not any(c.startswith("License ::") for c in project["classifiers"])
    assert {"Issues", "Changelog"} <= set(project["urls"])
    shapely = next(d for d in project["dependencies"] if d.startswith("shapely"))
    assert re.fullmatch(r"shapely>=2\.0\.(\d+)", shapely) and int(shapely.rsplit(".", 1)[1]) >= 4


def _compose() -> dict:
    import ruamel.yaml

    return ruamel.yaml.YAML(typ="safe").load((ROOT / "docker-compose.yml").read_text())


def test_compose_beast_port_and_hardening():
    services = _compose()["services"]
    web = services["web"]
    assert web["environment"]["PYAERIAL_BEAST_PORT"] == "30105"
    assert any(p.endswith(":30105") and "BEAST_PUBLISH_PORT:-30105" in p for p in web["ports"])
    assert web["ulimits"]["nofile"]["soft"] >= 4096 and web["mem_limit"]
    redis = services["redis"]
    assert redis["read_only"] is True and redis["mem_limit"]
    command = services["dump1090"]["command"]
    for flag in ("--net-ri-port", "--net-bi-port"):
        assert command[command.index(flag) + 1] == "0"
    for name, service in services.items():
        options = service["logging"]["options"]
        assert options["max-size"] and options["max-file"], name
    # Passwords never appear in a service's environment or command line.
    for name, service in services.items():
        text = repr(service.get("environment", {})) + repr(service.get("command", ""))
        assert "${REDIS_PASSWORD" not in text, name


def test_run_engine_disables_dump1090_inputs():
    script = (ROOT / "scripts" / "run-engine.sh").read_text()
    assert "--net-ri-port 0 --net-bi-port 0" in script


def test_image_ships_the_example_config_not_the_local_one():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY src/pyaerial/examples/config.yaml /opt/PyAerial/config.yaml" in dockerfile
    assert "\nCOPY config.yaml" not in dockerfile
    assert "/config.yaml" in (ROOT / ".dockerignore").read_text().splitlines()


def test_package_data_lists_captures_and_static_explicitly():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]
    patterns = data["package-data"]["pyaerial"]
    assert "examples/captures/*" in patterns
    assert any(p.startswith("static/") for p in patterns)
