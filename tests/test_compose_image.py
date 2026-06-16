"""Guard test for the prebuilt-image compose file.

Pure-filesystem check (no app, no network, no fixtures): the pinned watchmend
image tag in docker-compose.image.yml must match the version declared in
pyproject.toml — otherwise a release bumps the package but the README's
image-based deploy keeps pulling the previous version.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_IMAGE = ROOT / "docker-compose.image.yml"
PYPROJECT = ROOT / "pyproject.toml"

_IMAGE_TAG = re.compile(r"ghcr\.io/hyxiaoge/watchmend:(\d+\.\d+\.\d+)")


def _pyproject_version() -> str:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


def test_compose_image_file_exists():
    assert COMPOSE_IMAGE.is_file(), "missing docker-compose.image.yml"


def test_compose_image_tag_matches_pyproject():
    # pin 必须等于 pyproject 版本(发版 bump 时同步更新,见 RELEASING.md)
    tags = _IMAGE_TAG.findall(COMPOSE_IMAGE.read_text(encoding="utf-8"))
    assert tags, "docker-compose.image.yml has no pinned watchmend image tag"
    pv = _pyproject_version()
    assert all(t == pv for t in tags), f"compose tag(s) {tags} != pyproject {pv}"
