"""What a HACS release ships besides the code: brand images, hacs.json, version."""

import json
import re
import tomllib
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).parent.parent
INTEGRATION = ROOT / "custom_components" / "beurer_scales"


def test_brand_icons_are_square_pngs_of_the_brand_sizes() -> None:
    # HA serves custom integrations' brand images from `brand/` (HA 2026.3+), and
    # HACS checks for `brand/icon.png`. Brands spec: icon 256 px, @2x 512 px.
    for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
        with Image.open(INTEGRATION / "brand" / name) as image:
            assert image.format == "PNG"
            assert image.size == (size, size)
            assert image.mode == "RGBA"


def test_hacs_json_uses_only_keys_hacs_knows_and_the_minimum_ha() -> None:
    # Keys from the HACS publishing docs; the minimum is ADR 0007's 2026.10.
    known = {
        "name",
        "content_in_root",
        "zip_release",
        "filename",
        "hide_default_branch",
        "country",
        "homeassistant",
        "hacs",
        "persistent_directory",
    }
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert set(hacs) <= known
    assert hacs["name"] == "Beurer Scales"
    assert hacs["homeassistant"] == "2026.10.0"


def test_manifest_has_a_release_version_matching_the_project() -> None:
    # The release tag is `v<version>`; a dev or pre-release suffix doesn't belong
    # in a published release.
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert manifest["version"] == project["version"]
