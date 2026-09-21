from importlib.resources import files

from deepreefmap.camera.intrinsics import CameraProfile, available_profile_names
from deepreefmap.config.classes import load_classes

# The profiles the pipeline ships. The registry mirrors this list by hand in
# `deepreefmap-api/src/contract/preset_schema.rs::CAMERA_PROFILES`, and the
# library's package data is a glob, so a new one ships in silence otherwise.
BUNDLED_CAMERA_PROFILES = ("gopro_hero_10",)


def test_default_classes_and_camera_profiles_load_outside_repo_root(tmp_path, monkeypatch) -> None:
    # The packaged binary runs from an arbitrary working directory; the
    # library's bundled resources must resolve without a repo checkout.
    monkeypatch.chdir(tmp_path)

    classes = load_classes()
    profile = CameraProfile.load("gopro_hero_10")

    assert classes.classes
    assert profile.image_size == (1920, 1080)
    assert "gopro_hero_10" in available_profile_names()


def test_the_macos_bundle_icon_ships() -> None:
    # The macOS wrapper bundle writes this straight out of package data, so that
    # a shortcut can be created on a host with no sips/iconutil. Missing from
    # package-data it would degrade silently to a generic Dock icon.
    from importlib import resources

    icon = resources.files("deepreefmap_gui.resources").joinpath("icon.icns")
    assert icon.is_file()
    assert icon.read_bytes()[:4] == b"icns"


def test_bundled_fonts_are_present() -> None:
    # The GUI pins a global Inter font and a JetBrains Mono monospace; both
    # must ship as package data or the app silently falls back to per-OS system
    # fonts (the macOS/Linux size mismatch this was meant to fix).
    from importlib import resources

    from deepreefmap_gui.core.fonts import _FONT_FILES

    fonts_dir = resources.files("deepreefmap_gui.resources").joinpath("fonts")
    for name in _FONT_FILES:
        assert fonts_dir.joinpath(name).is_file(), f"missing bundled font {name}"


def test_the_bundled_camera_profiles_are_the_ones_the_registry_mirrors() -> None:
    """Read from the package rather than `available_profile_names()`, which merges
    in whatever the machine has calibrated."""
    shipped = sorted(
        entry.name.removesuffix(".json")
        for entry in files("deepreefmap.resources.camera_profiles").iterdir()
        if entry.name.endswith(".json")
    )

    assert shipped == sorted(BUNDLED_CAMERA_PROFILES), (
        "the pipeline's bundled camera profiles changed. "
        "deepreefmap-api/src/contract/preset_schema.rs::CAMERA_PROFILES hand-lists them "
        "and reaches contract/preset-schema.json: update it, re-export the contract, "
        "then update BUNDLED_CAMERA_PROFILES here."
    )
