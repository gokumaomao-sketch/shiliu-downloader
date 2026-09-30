"""The browser-extension build must produce a manifest each store will accept.

This has broken before: Chrome refuses to load an unpacked folder whose MV3
manifest carries ``background.scripts`` (Firefox's format), which is why the
per-browser manifests live in ``manifests/`` and are merged into exactly one
``manifest.json`` by ``build.js`` (node, for "Load unpacked") and by
``build_extension.py`` (python, for the store zips).

Everything here derives the merged manifests the same way the builders do, so
the tests hold even when ``browser_extension/manifest.json`` (gitignored) is
absent or stale.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = ROOT / "manifests"
EXT = ROOT / "browser_extension"

CHROME_OVERLAY = "manifest.chrome.json"
FIREFOX_OVERLAY = "manifest.firefox.json"
OVERLAYS = [CHROME_OVERLAY, FIREFOX_OVERLAY]

NODE = shutil.which("node")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _build_extension():
    """The real packaging module from the repo root (conftest puts ROOT on sys.path)."""
    if str(ROOT) not in sys.path:  # pragma: no cover - conftest normally does this
        sys.path.insert(0, str(ROOT))
    import build_extension

    return build_extension


def _merged(overlay_name: str) -> dict:
    """Merge exactly as build_extension._merged_manifest does, via that function."""
    return _build_extension()._merged_manifest(overlay_name)


def _load(name: str) -> dict:
    return json.loads((MANIFESTS / name).read_text(encoding="utf-8"))


def _referenced_files(manifest: dict) -> set[str]:
    """Every extension-relative file path the manifest points at."""
    refs: set[str] = set()

    background = manifest.get("background", {})
    if "service_worker" in background:
        refs.add(background["service_worker"])
    for script in background.get("scripts", []):
        refs.add(script)

    for entry in manifest.get("content_scripts", []):
        refs.update(entry.get("js", []))
        refs.update(entry.get("css", []))

    action = manifest.get("action", manifest.get("browser_action", {}))
    if action.get("default_popup"):
        refs.add(action["default_popup"])
    default_icon = action.get("default_icon", {})
    if isinstance(default_icon, str):
        refs.add(default_icon)
    else:
        refs.update(default_icon.values())

    refs.update(manifest.get("icons", {}).values())

    for war in manifest.get("web_accessible_resources", []):
        if isinstance(war, dict):
            refs.update(war.get("resources", []))
        else:
            refs.add(war)

    options_ui = manifest.get("options_ui", {})
    if options_ui.get("page"):
        refs.add(options_ui["page"])
    if manifest.get("options_page"):
        refs.add(manifest["options_page"])

    return refs


def _node_sandbox(tmp_path: Path) -> Path:
    """A throwaway copy of build.js + manifests/ so node never writes into the repo.

    build.js resolves everything from __dirname, so a copy of the script beside a
    copy of manifests/ and an empty browser_extension/ behaves exactly like the
    real tree.
    """
    shutil.copy(ROOT / "build.js", tmp_path / "build.js")
    shutil.copytree(MANIFESTS, tmp_path / "manifests")
    (tmp_path / "browser_extension").mkdir()
    return tmp_path


def _run_node(sandbox: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE, str(sandbox / "build.js"), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


requires_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")


# --------------------------------------------------------------------------
# the manifest sources
# --------------------------------------------------------------------------
def test_base_manifest_is_valid_json_with_a_version():
    base = _load("manifest.base.json")

    assert base["manifest_version"] == 3
    assert base["name"]
    assert base["version"], "the stores key every upload off manifest.version"


def test_base_version_is_a_store_acceptable_number():
    """Chrome/AMO require 1-4 dot-separated integers — "0.5.47-beta" is rejected."""
    parts = _load("manifest.base.json")["version"].split(".")

    assert 1 <= len(parts) <= 4
    assert all(p.isdigit() for p in parts), "version parts must be plain integers"


def test_base_declares_no_background_block():
    """`background` must come solely from an overlay, or the merge could leak the
    other browser's format through."""
    assert "background" not in _load("manifest.base.json")


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_overlay_is_valid_json_with_a_background_block(overlay):
    data = _load(overlay)

    assert isinstance(data, dict)
    assert isinstance(data.get("background"), dict), f"{overlay} must define background"


# --------------------------------------------------------------------------
# merged CHROME manifest
# --------------------------------------------------------------------------
def test_chrome_manifest_uses_a_service_worker():
    background = _merged(CHROME_OVERLAY)["background"]

    assert background.get("service_worker") == "background.js"


def test_chrome_manifest_has_no_background_scripts():
    """The exact key that makes Chrome refuse to load the unpacked folder."""
    assert "scripts" not in _merged(CHROME_OVERLAY)["background"]


def test_chrome_manifest_has_no_browser_specific_settings():
    """Chrome errors out on the gecko-only block."""
    assert "browser_specific_settings" not in _merged(CHROME_OVERLAY)


def test_chrome_manifest_is_mv3():
    assert _merged(CHROME_OVERLAY)["manifest_version"] == 3


# --------------------------------------------------------------------------
# merged FIREFOX manifest
# --------------------------------------------------------------------------
def test_firefox_manifest_uses_background_scripts():
    background = _merged(FIREFOX_OVERLAY)["background"]

    assert background.get("scripts") == ["background.js"]


def test_firefox_manifest_has_no_service_worker():
    """Firefox MV3 uses event pages; a service_worker key breaks the AMO build."""
    assert "service_worker" not in _merged(FIREFOX_OVERLAY)["background"]


def test_firefox_manifest_has_a_gecko_id():
    gecko = _merged(FIREFOX_OVERLAY)["browser_specific_settings"]["gecko"]

    assert "@" in gecko["id"], "AMO needs a stable extension id"


def test_firefox_manifest_declares_data_collection_permissions():
    """AMO now rejects a submission whose gecko block omits this key."""
    gecko = _merged(FIREFOX_OVERLAY)["browser_specific_settings"]["gecko"]
    dcp = gecko["data_collection_permissions"]

    assert isinstance(dcp.get("required"), list)
    assert dcp["required"], "an empty required list means 'none' must be stated explicitly"


# --------------------------------------------------------------------------
# permissions
# --------------------------------------------------------------------------
@pytest.mark.parametrize("overlay", OVERLAYS)
def test_cookies_stays_opt_in(overlay):
    """Store review passed only because cookie access is requested at runtime."""
    manifest = _merged(overlay)

    assert "cookies" in manifest["optional_permissions"]
    assert "cookies" not in manifest["permissions"]


# --------------------------------------------------------------------------
# version lockstep
# --------------------------------------------------------------------------
@pytest.mark.parametrize("overlay", OVERLAYS)
def test_extension_version_matches_the_app_version(overlay):
    import magic_downloader

    assert _merged(overlay)["version"] == magic_downloader.__version__


# --------------------------------------------------------------------------
# referenced files really ship
# --------------------------------------------------------------------------
@pytest.mark.parametrize("overlay", OVERLAYS)
def test_every_referenced_file_exists(overlay):
    manifest = _merged(overlay)
    refs = _referenced_files(manifest)

    assert refs, "the collector found nothing — the manifest shape must have changed"
    missing = sorted(r for r in refs if not (EXT / r).is_file())
    assert not missing, f"{overlay} references files absent from browser_extension/: {missing}"


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_reference_collector_notices_a_missing_file(overlay):
    """Guard for the test above: it must actually fail when a file goes missing."""
    manifest = _merged(overlay)
    manifest["icons"]["128"] = "icons/nope.png"

    refs = _referenced_files(manifest)
    assert "icons/nope.png" in refs
    assert not (EXT / "icons/nope.png").exists()


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_zip_asset_list_covers_everything_the_manifest_references(overlay):
    """A manifest reference outside ASSETS would be silently left out of the zip."""
    build_extension = _build_extension()

    uncovered = sorted(_referenced_files(_merged(overlay)) - set(build_extension.ASSETS))
    assert not uncovered, f"build_extension.ASSETS is missing {uncovered}"


def test_popup_html_script_is_packaged():
    """popup.js is invisible to the manifest — only popup.html loads it."""
    build_extension = _build_extension()
    html = (EXT / "popup.html").read_text(encoding="utf-8")

    assert 'src="popup.js"' in html
    assert "popup.js" in build_extension.ASSETS
    assert (EXT / "popup.js").is_file()


# --------------------------------------------------------------------------
# the python packager
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "overlay,present,absent",
    [(CHROME_OVERLAY, "service_worker", "scripts"), (FIREFOX_OVERLAY, "scripts", "service_worker")],
)
def test_zip_holds_a_root_manifest_for_the_right_browser(tmp_path, overlay, present, absent):
    build_extension = _build_extension()
    out = tmp_path / f"{overlay}.zip"

    build_extension._write_zip(out, overlay)

    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        manifest = json.loads(z.read("manifest.json"))

    assert "manifest.json" in names, "stores require the manifest at the archive root"
    assert not [n for n in names if n.endswith("manifest.chrome.json")]
    assert not [n for n in names if n.endswith("manifest.firefox.json")]
    assert present in manifest["background"]
    assert absent not in manifest["background"]


def test_zip_contains_every_declared_asset(tmp_path):
    build_extension = _build_extension()
    out = tmp_path / "chrome.zip"

    build_extension._write_zip(out, CHROME_OVERLAY)

    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
    assert set(build_extension.ASSETS) <= names


def test_zipped_manifest_matches_the_in_memory_merge(tmp_path):
    build_extension = _build_extension()
    out = tmp_path / "firefox.zip"

    build_extension._write_zip(out, FIREFOX_OVERLAY)

    with zipfile.ZipFile(out) as z:
        assert json.loads(z.read("manifest.json")) == _merged(FIREFOX_OVERLAY)


def test_python_packager_does_not_reject_a_cross_contaminated_overlay(tmp_path, monkeypatch):
    """Documents a REAL GAP (not fixed here).

    build.js refuses to write a Chrome manifest containing background.scripts,
    but build_extension.py performs no such check — it will happily zip an
    unloadable Chrome package. See the node guard test below for the behaviour
    the python packager is missing.
    """
    build_extension = _build_extension()
    fake = tmp_path / "manifests"
    fake.mkdir()
    shutil.copy(MANIFESTS / "manifest.base.json", fake / "manifest.base.json")
    (fake / "manifest.chrome.json").write_text(
        json.dumps({"background": {"scripts": ["background.js"]}}), encoding="utf-8"
    )
    monkeypatch.setattr(build_extension, "MANIFESTS", fake)

    merged = build_extension._merged_manifest("manifest.chrome.json")

    assert merged["background"] == {"scripts": ["background.js"]}, (
        "current behaviour: a Chrome build with Firefox's background format is "
        "produced without complaint"
    )


def test_python_packager_skips_missing_assets_instead_of_failing(tmp_path, monkeypatch):
    """Documents a REAL GAP (not fixed here): a typo'd/renamed asset is merely
    printed as `! missing` and the store zip is written without it."""
    build_extension = _build_extension()
    monkeypatch.setattr(build_extension, "ASSETS", ["background.js", "not_a_real_file.js"])
    out = tmp_path / "chrome.zip"

    build_extension._write_zip(out, CHROME_OVERLAY)  # current behaviour: no exception

    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
    assert "background.js" in names
    assert "not_a_real_file.js" not in names


# --------------------------------------------------------------------------
# the node builder (build.js)
# --------------------------------------------------------------------------
@requires_node
@pytest.mark.parametrize(
    "flag,overlay,present,absent",
    [
        ("--chrome", CHROME_OVERLAY, "service_worker", "scripts"),
        ("--firefox", FIREFOX_OVERLAY, "scripts", "service_worker"),
    ],
)
def test_node_build_writes_the_right_background_format(tmp_path, flag, overlay, present, absent):
    sandbox = _node_sandbox(tmp_path)

    proc = _run_node(sandbox, flag)

    assert proc.returncode == 0, proc.stderr
    manifest = json.loads((sandbox / "browser_extension" / "manifest.json").read_text("utf-8"))
    assert present in manifest["background"]
    assert absent not in manifest["background"]
    assert manifest == _merged(overlay), "node and python builders must agree"


@requires_node
def test_node_build_leaves_no_per_browser_manifest_in_the_extension_folder(tmp_path):
    """The stale-file cleanup: Chrome must not find a `scripts` key anywhere."""
    sandbox = _node_sandbox(tmp_path)
    ext = sandbox / "browser_extension"
    shutil.copy(MANIFESTS / FIREFOX_OVERLAY, ext / FIREFOX_OVERLAY)

    proc = _run_node(sandbox, "--chrome")

    assert proc.returncode == 0, proc.stderr
    assert not (ext / FIREFOX_OVERLAY).exists()
    assert not (ext / CHROME_OVERLAY).exists()


@requires_node
def test_node_build_refuses_a_chrome_overlay_carrying_background_scripts(tmp_path):
    sandbox = _node_sandbox(tmp_path)
    (sandbox / "manifests" / CHROME_OVERLAY).write_text(
        json.dumps({"background": {"scripts": ["background.js"]}}), encoding="utf-8"
    )

    proc = _run_node(sandbox, "--chrome")

    assert proc.returncode != 0
    assert "scripts" in proc.stderr
    assert not (sandbox / "browser_extension" / "manifest.json").exists()


@requires_node
def test_node_build_refuses_a_firefox_overlay_carrying_a_service_worker(tmp_path):
    sandbox = _node_sandbox(tmp_path)
    (sandbox / "manifests" / FIREFOX_OVERLAY).write_text(
        json.dumps({"background": {"service_worker": "background.js"}}), encoding="utf-8"
    )

    proc = _run_node(sandbox, "--firefox")

    assert proc.returncode != 0
    assert not (sandbox / "browser_extension" / "manifest.json").exists()


@requires_node
@pytest.mark.parametrize("args", [(), ("--chrome", "--firefox"), ("--safari",)])
def test_node_build_requires_exactly_one_target(tmp_path, args):
    sandbox = _node_sandbox(tmp_path)

    proc = _run_node(sandbox, *args)

    assert proc.returncode != 0
    assert not (sandbox / "browser_extension" / "manifest.json").exists()


@requires_node
def test_node_build_rejects_a_malformed_overlay(tmp_path):
    sandbox = _node_sandbox(tmp_path)
    (sandbox / "manifests" / CHROME_OVERLAY).write_text("{ not json", encoding="utf-8")

    proc = _run_node(sandbox, "--chrome")

    assert proc.returncode != 0
    assert not (sandbox / "browser_extension" / "manifest.json").exists()
