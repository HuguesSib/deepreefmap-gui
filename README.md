# DeepReefMap GUI
The desktop application for [deepreefmap](https://github.com/eceo-epfl/deepreefmap): 3D semantic mapping of coral reefs from dive footage.

## Getting started

Download the build for your platform from the [releases page](https://github.com/eceo-epfl/deepreefmap-gui/releases).

| Platform | File | Variants |
|---|---|---|
| Windows | `deepreefmap-gui-setup-windows-x64-<version>.exe` | `-cu130` for RTX 50-series |
| macOS (Apple Silicon) | `deepreefmap-gui-macos-arm64-<version>.dmg` | |
| Linux | `deepreefmap-gui-linux-x64-<version>` | `-cu130` for RTX 50-series, `-rocm` for AMD |

macOS builds are unsigned, so the first launch needs System Settings > Privacy & Security > "Open Anyway"; Linux builds need `chmod +x`. The first launch provisions its own Python environment (several GB). Updates and rollbacks are under Setup.

Four destinations, none a prerequisite for another. **Transects** are the lines you survey, with the cover and repeatability their repeat passes agree on. **Videos** is the footage itself, grouped by the day it was shot: every clip, what has been cut from it, and whether the file is still where you left it. **Cart** queues passes for the next session and runs them: add them from anywhere in the app, then Start processing checks the cart out as a session, and reruns land beside their originals in Browse. **Browse** is everything produced so far, grouped by session, transect or run. **Setup** covers whether the laptop can process a dive, the models installed on it, and what it is doing while it runs. Models are downloaded there, or imported from a USB pack for machines that stay offline; the `coralscapes-*` models need a free Hugging Face account.

## Glossary

Catalogue:

- **Site**: a named place on a reef, with country and a map point. Transects belong to a site.
- **Campaign**: one trip. A repeat visit is a new campaign.
- **Transect**: a tape line at a site: end points where known, tape length, depth. Optional on a pass; without one the run is unscaled and skipped by the repeatability comparison.
- **Survey event**: all passes of one transect in one campaign. Derived; nothing to create.
- **Validated**: checked in the console. From then on a change made here is a proposal the console accepts or dismisses. Until then, only the laptop that made a row can change it.
- **Proposal**: a change made here that the console has not accepted, because the row was validated, deleted, or edited there meanwhile. Nothing sent from here is ever lost.

Footage:

- **Video (clip)**: a file off the camera, listed under Videos. Identity is its content hash, so a moved file is still the same clip. GoPro chapters of one recording are separate files, one swim. A clip carries which camera of the rig shot it, where that camera sat, whether it was mounted upside down, and a review verdict.
- **Pass**: a cutout of a video, the unit everything else works on. Identity is `video + start/end time + direction`. One traversal of a transect, when there is one, on a day, in a campaign.
- **Gravity**: the `GRAV` stream a GoPro records beside the footage, which the mapping backends use to stand a reconstruction upright. Read from the file's own index on import, so Videos says yes, no, or nothing at all where it could not be read.

Queueing:

- **Cart**: the newest un-started session, filled from anywhere via Add to cart. The header's Cart button counts it.
- **Checkout**: Start processing. Turns the cart into an order by placing a run for every queued pass.
- **Order**: a started session. Membership is closed; the only mid-run controls are pause, cancel, and Hold on a pass the worker has not reached.
- **Next session**: the cart assembled while an order runs, under its own divider on the Cart page. Startable once the order finishes.
- **Held**: a pass kept in its session but skipped when processing starts.

Results:

- **Run**: one reconstruction of one pass, in its own directory. A rerun is a second run of the same pass; repeats are the reproducibility data.
- **Attempt**: one run among several of the same pass, numbered by its directory suffix (`__r02`, `__r03`, ...) and listed under the pass node in Browse.
- **Session**: the set of runs placed together, usually a dive or a day. A run records its session; a pass only records where it was first catalogued. Never leaves this laptop.

## Settings

Runs are configured from a preset YAML, `deepreefmap_gui/resources/configs/survey_preset.yaml`. `segmentation_name` picks the segmentation model (`coralscapes-vit-{s,b,l}-dpt`, `segformer-b{2,5}`) and `mapping_name` the reconstruction backend (`loger_star` and `loger` need a GPU, `scsfmlearner` runs on CPU). Transect length and time trim come from the survey database, not the preset. To standardise several machines on one configuration, point them at a shared copy with `DEEPREEFMAP_SURVEY_PRESET`; the file's header comment covers the rest.

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
