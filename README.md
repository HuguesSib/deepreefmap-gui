# DeepReefMap GUI
The desktop application for [deepreefmap](https://github.com/eceo-epfl/deepreefmap): 3D semantic mapping of coral reefs from dive footage.

## Getting started

Download the build for your platform from the [releases page](https://github.com/eceo-epfl/deepreefmap-gui/releases).

| Platform | File | Variants |
|---|---|---|
| Windows | `deepreefmap-gui-setup-windows-x64-<version>.exe` | `-cu130` for RTX 50-series |
| macOS (Apple Silicon) | `deepreefmap-gui-macos-arm64-<version>.dmg` | |
| Linux | `deepreefmap-gui-linux-x64-<version>` | `-cu130` for RTX 50-series, `-rocm` for AMD |

macOS builds are unsigned: the first launch needs System Settings > Privacy & Security > "Open Anyway". Linux builds need `chmod +x`. The first launch provisions its own Python environment (several GB). Updates and rollbacks are under Setup.

Five destinations, none a prerequisite for another:

- **Transects**: the lines you survey, with the cover and repeatability of their repeat passes.
- **Videos**: the footage, grouped by the day it was shot: every clip, what has been cut from it, and whether the file is still there.
- **Cart**: passes queued for the next session. Start processing checks the cart out as a session; reruns land beside their originals in Browse.
- **Browse**: everything produced so far, grouped by session, transect or run.
- **Setup**: whether the laptop can process a dive, the models installed on it, and what it is doing while it runs. Models download there or import from a USB pack; the `coralscapes-*` models need a free Hugging Face account.

## Glossary

Catalogue:

- **Site**: a named place on a reef, with country and a map point. Transects belong to a site.
- **Campaign**: one trip. A repeat visit is a new campaign.
- **Transect**: a tape line at a site, with end points, tape length and depth. Optional on a pass.
- **Survey event**: all passes of one transect in one campaign. Derived; nothing to create.
- **Validated**: checked in the console. Later changes made here become proposals.
- **Proposal**: a change made here to a row the console validated, deleted or edited meanwhile. The console accepts or dismisses it.

Footage:

- **Video (clip)**: a file off the camera, identified by its content hash. Carries camera, rig position, mounting and a review verdict.
- **Pass**: a cutout of a video, identified by `video + start/end time + direction`. One traversal of a transect on a day in a campaign.
- **Gravity**: the `GRAV` stream a GoPro records beside the footage. Read on import; Videos shows yes, no or unread.

Queueing:

- **Cart**: the newest un-started session, filled from anywhere via Add to cart.
- **Checkout**: Start processing. Places a run for every queued pass.
- **Order**: a started session. Membership is closed; mid-run controls are pause, cancel and Hold.
- **Next session**: the cart assembled while an order runs. Startable once the order finishes.
- **Held**: a pass kept in its session but skipped when processing starts.

Results:

- **Run**: one reconstruction of one pass, in its own directory. Repeats are the reproducibility data.
- **Attempt**: one run among several of the same pass, numbered by directory suffix (`__r02`, `__r03`, ...).
- **Session**: the set of runs placed together, usually a dive or a day. Never leaves this laptop.

## Settings

Runs are configured from a preset YAML, `deepreefmap_gui/resources/configs/survey_preset.yaml`:

- `segmentation_name`: the segmentation model (`coralscapes-vit-{s,b,l}-dpt`, `segformer-b{2,5}`).
- `mapping_name`: the reconstruction backend (`loger_star` and `loger` need a GPU, `scsfmlearner` runs on CPU).
- Transect length and time trim come from the survey database, not the preset.
- `DEEPREEFMAP_SURVEY_PRESET` points several machines at one shared copy; the file's header comment covers the rest.

## Development

deepreefmap resolves from the commit pinned in `pyproject.toml`, so no separate checkout is needed.

```bash
uv sync --extra dev          # add --extra cu126, cu130 or rocm for GPU
uv run deepreefmap-gui
uv run pytest                # xvfb-run -a uv run pytest if the viewer tests crash headless
```

`scripts/build.sh` (Linux/macOS) and `scripts/build.ps1` (Windows) produce the release binaries.

## License

Apache-2.0. Qt bindings via PySide6 (LGPL); bundled fonts and third-party components are listed in `THIRD_PARTY_NOTICES.md`.
