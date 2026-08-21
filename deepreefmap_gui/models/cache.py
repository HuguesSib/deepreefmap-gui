"""Every weight the app can fetch, and what this disk already has of it.

The single source of truth for both: the model cards render one row per `ALL_MODELS` entry, and
the Run step gates its button on `is_model_cached` for the selected backends. A weight missing
from these lists still loads, it just downloads mid-run instead, which is fatal on a field laptop
that has already gone offline.

Two kinds of entry do not describe themselves:

- **A DPT head needs its backbone repo listed alongside it.** `EPFL-ECEO/coralscapes-vit-*-dpt`
  ships the head and a loader that calls `from_pretrained` on the DINOv3 encoder named in its
  `config.json#encoder_id`, so a head cached on its own still reaches for the network on first
  use. Both repos in `hf_repos` means one download covers the pair.
- **LoGeR reads its checkpoints from a fixed path**, not by repo id, so its `materialise_to` maps
  snapshot-relative files to that path and `prefetch_model` symlinks them there afterwards
  (copying where symlinks are unavailable). `is_model_cached` stays False until every destination
  exists, which is what stops a half-materialised model from looking ready.

Qt-free: the cards, the download progress and the login prompt are `models/cache_ui.py`. Free
disk is checked before the network is touched, so a thin SSD gets a refusal rather than a partial
cache that confuses the next launch.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from deepreefmap.paths import loger_ckpts_dir
from huggingface_hub.constants import HF_HUB_CACHE

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]

_HF_CACHE_ROOT = Path(HF_HUB_CACHE)

# LoGeR checkpoints live outside the HF cache (the backend loads them from a fixed
# path, not by repo id). Materialised here after snapshot_download; must be
# user-writable, so it's a platformdirs dir (see deepreefmap.paths), not the
# read-only install tree.
_LOGER_CKPTS = loger_ckpts_dir()

# Refuse to start a download when free disk under the HF cache mount is below
# this threshold. The DINOv3-L head + backbone alone are ~2.5 GB; leaving a
# comfortable margin avoids half-written caches on field laptops with thin SSDs.
_MIN_FREE_BYTES = 10 * 1024**3


class DownloadCancelled(Exception):
    """Raised from a progress callback to abort an in-flight download."""


class InsufficientDiskSpace(RuntimeError):
    """Raised before download when free disk under the HF cache is too low."""


@dataclass
class DeletionResult:
    """What delete_model removed, and what it left behind for a sibling."""

    revisions_removed: int
    # repo id -> names of the installed models that still need it.
    kept_repos: dict[str, list[str]]

    def kept_summary(self) -> str:
        """One phrase naming the models the kept repos were kept for."""
        names = sorted({n for names in self.kept_repos.values() for n in names})
        return ", ".join(names)


@dataclass
class ModelInfo:
    name: str
    kind: str
    hf_repos: list[str]
    gated: bool
    description: str
    approx_size_mb: int | None = None
    release_date: str | None = None
    # Optional copy step run after snapshot_download. Maps repo-relative
    # paths inside the snapshot to absolute destinations the runtime backend
    # reads from. Used for LoGeR, which loads checkpoints from a fixed
    # user-writable path (see deepreefmap.paths.loger_ckpts_dir), not the HF cache.
    materialise_to: dict[str, Path] = field(default_factory=dict)
    # Name of an optional install extra this model needs (e.g. "loger"). When
    # the extra isn't installed the UI shows the model disabled with a hint
    # rather than a Download button. See model_available().
    requires_extra: str | None = None


SEGMENTATION_MODELS: list[ModelInfo] = [
    ModelInfo(
        name="segformer-b2",
        kind="segmentation",
        hf_repos=["EPFL-ECEO/segformer-b2-finetuned-coralscapes-1024-1024"],
        gated=False,
        description="SegFormer B2 (lightweight, no auth required)",
        approx_size_mb=110,
        release_date="2025-03-07",
    ),
    ModelInfo(
        name="segformer-b5",
        kind="segmentation",
        hf_repos=["EPFL-ECEO/segformer-b5-finetuned-coralscapes-1024-1024"],
        gated=False,
        description="SegFormer B5 (larger, no auth required)",
        approx_size_mb=339,
        release_date="2025-03-21",
    ),
    # The coralscapes-vit-*-dpt repos only ship the DPT head plus a custom
    # loader. The loader's from_pretrained() pulls a Meta DINOv3 backbone
    # named in config.json#encoder_id the first time it runs, so the backbone
    # repo is listed alongside the head to keep offline laptops self-sufficient.
    ModelInfo(
        name="coralscapes-vit-s-dpt",
        kind="segmentation",
        hf_repos=[
            "EPFL-ECEO/coralscapes-vit-s-dpt",
            "facebook/dinov3-vits16-pretrain-lvd1689m",
        ],
        gated=True,
        description="DINOv3 ViT-S DPT (requires HF login)",
        approx_size_mb=257,
        release_date="2026-05-06",
    ),
    ModelInfo(
        name="coralscapes-vit-b-dpt",
        kind="segmentation",
        hf_repos=[
            "EPFL-ECEO/coralscapes-vit-b-dpt",
            "facebook/dinov3-vitb16-pretrain-lvd1689m",
        ],
        gated=True,
        description="DINOv3 ViT-B DPT (requires HF login)",
        approx_size_mb=786,
        release_date="2026-04-22",
    ),
    ModelInfo(
        name="coralscapes-vit-l-dpt",
        kind="segmentation",
        hf_repos=[
            "EPFL-ECEO/coralscapes-vit-l-dpt",
            "facebook/dinov3-vitl16-pretrain-lvd1689m",
        ],
        gated=True,
        description="DINOv3 ViT-L DPT (largest, requires HF login)",
        approx_size_mb=2542,
        release_date="2026-04-23",
    ),
]

MAPPING_MODELS: list[ModelInfo] = [
    ModelInfo(
        name="scsfmlearner",
        kind="mapping",
        hf_repos=["EPFL-ECEO/deepreefmap-sfm-net"],
        gated=False,
        description="SC-SfMLearner depth + pose estimation",
        approx_size_mb=326,
        release_date="2026-05-06",
    ),
    ModelInfo(
        name="loger",
        kind="mapping",
        hf_repos=["Junyi42/LoGeR"],
        gated=False,
        description="LoGeR depth + pose estimation (GPU required)",
        approx_size_mb=4787,
        release_date="2026-03-06",
        materialise_to={
            "LoGeR/latest.pt": _LOGER_CKPTS / "LoGeR" / "latest.pt",
            "LoGeR/original_config.yaml": _LOGER_CKPTS / "LoGeR" / "original_config.yaml",
        },
        requires_extra="loger",
    ),
    ModelInfo(
        name="loger_star",
        kind="mapping",
        hf_repos=["Junyi42/LoGeR"],
        gated=False,
        description="LoGeR* (longer-context variant, GPU required)",
        approx_size_mb=4787,
        release_date="2026-03-06",
        materialise_to={
            "LoGeR_star/latest.pt": _LOGER_CKPTS / "LoGeR_star" / "latest.pt",
            "LoGeR_star/original_config.yaml": _LOGER_CKPTS / "LoGeR_star" / "original_config.yaml",
        },
        requires_extra="loger",
    ),
]

BACKBONE_MODELS: list[ModelInfo] = [
    ModelInfo(
        name="dinov3-vits16",
        kind="backbone",
        hf_repos=["facebook/dinov3-vits16-pretrain-lvd1689m"],
        gated=True,
        description="DINOv3 ViT-S backbone (needed by coralscapes-vit-s-dpt)",
        approx_size_mb=85,
    ),
    ModelInfo(
        name="dinov3-vitb16",
        kind="backbone",
        hf_repos=["facebook/dinov3-vitb16-pretrain-lvd1689m"],
        gated=True,
        description="DINOv3 ViT-B backbone (needed by coralscapes-vit-b-dpt)",
        approx_size_mb=330,
    ),
    ModelInfo(
        name="dinov3-vitl16",
        kind="backbone",
        hf_repos=["facebook/dinov3-vitl16-pretrain-lvd1689m"],
        gated=True,
        description="DINOv3 ViT-L backbone (needed by coralscapes-vit-l-dpt)",
        approx_size_mb=1170,
    ),
]

# DPT model name → backbone model name
DPT_BACKBONE_MAP: dict[str, str] = {
    "coralscapes-vit-s-dpt": "dinov3-vits16",
    "coralscapes-vit-b-dpt": "dinov3-vitb16",
    "coralscapes-vit-l-dpt": "dinov3-vitl16",
}

def required_model_names(settings: Mapping[str, Any]) -> set[str]:
    """The model names a run under these settings would load.

    The settings-dict twin of cache_ui's
    ModelManagementMixin._required_model_names, which reads the same answer off
    the run form's widgets: the mapping backend, and unless segmentation is
    skipped, the segmentation model plus the DINOv3 backbone a DPT head fetches
    at first use. Change the two together.
    """
    required = {str(settings.get("mapping_name") or "")}
    if not settings.get("skip_segmentation"):
        seg = str(settings.get("segmentation_name") or "")
        required.add(seg)
        backbone = DPT_BACKBONE_MAP.get(seg)
        if backbone:
            required.add(backbone)
    required.discard("")
    return required


# Mapping backends with no processor fallback: without a card they do not run
# slowly, they do not run at all. Every gate reads this one list rather than
# keeping a tuple literal of its own: the Run step and the setup step's
# readiness row disagreeing about which backends need a GPU is the failure this
# is preventing.
GPU_ONLY_BACKENDS: frozenset[str] = frozenset({"loger", "loger_star"})

ALL_MODELS = SEGMENTATION_MODELS + MAPPING_MODELS + BACKBONE_MODELS

# Models discovered at run time via discover_models(). Session-scoped (not
# persisted): re-running discovery is cheap and avoids a stale on-disk cache.
_DISCOVERED_MODELS: list[ModelInfo] = []
_DISCOVERED_LOCK = threading.Lock()


def discovered_models() -> list[ModelInfo]:
    with _DISCOVERED_LOCK:
        return list(_DISCOVERED_MODELS)


def all_known_models() -> list[ModelInfo]:
    """Hardcoded catalogue plus anything discovered this session."""
    return ALL_MODELS + discovered_models()


def segmentation_model_names() -> list[str]:
    """The segmentation dropdown's items, without importing the library registry.

    Same names, same order as deepreefmap.segmentation.registry's
    list_segmentation_models -- discovery registers into both, and
    tests/models/test_model_catalogue.py compares the two lists. Sourced here
    because that registry imports torch at module scope, and the form is built
    before the window is on screen.
    """
    names = {m.name for m in SEGMENTATION_MODELS}
    names.update(m.name for m in discovered_models() if m.kind == "segmentation")
    return sorted(names)


def model_available(info: ModelInfo) -> bool:
    """False when the model needs an install extra that isn't present."""
    if info.requires_extra == "loger":
        from deepreefmap.mapping.registry import loger_available
        return loger_available()
    return True


def register_discovered(info: ModelInfo) -> bool:
    """Add a discovered model to the session list; True if new, so discovery is idempotent."""
    with _DISCOVERED_LOCK:
        known = {m.name for m in ALL_MODELS} | {m.name for m in _DISCOVERED_MODELS}
        if info.name in known:
            return False
        _DISCOVERED_MODELS.append(info)
        return True


def discover_models() -> tuple[list[str], str | None]:
    """Query the EPFL-ECEO org for known-loadable models; returns (new_names, error)."""
    # Failures come back as a string rather than an exception: the caller is a
    # worker thread and cannot raise across the boundary.
    try:
        from deepreefmap.segmentation.registry import register_segmentation_model
        from huggingface_hub import HfApi

        from deepreefmap_gui.models.families import synthesize_model_info

        repos = HfApi().list_models(author="EPFL-ECEO")
    except Exception as exc:  # network, auth, or API errors
        return [], str(exc)[:200]

    new: list[str] = []
    for repo in repos:
        synth = synthesize_model_info(repo.id)
        if synth is None:
            continue
        info, resolution, family = synth
        if info.release_date is None and getattr(repo, "created_at", None) is not None:
            info.release_date = str(repo.created_at.date())
        if not register_discovered(info):
            continue
        register_segmentation_model(info.name, info.hf_repos[0], family, resolution)
        new.append(info.name)
    return new, None


def _hf_cache_dir(repo_id: str) -> Path:
    return _HF_CACHE_ROOT / f"models--{repo_id.replace('/', '--')}"


class ModelStatus(str, Enum):
    """Verified on-disk state of a model's cache; COMPLETE is the only usable one."""

    # PARTIAL covers the trap an interrupted download leaves: the cache dir exists
    # and config.json loads, but the weights never landed and the app dies mid-run.
    COMPLETE = "complete"
    PARTIAL = "partial"
    ABSENT = "absent"


# A repo is "downloaded" only once at least one of these lands. config.json alone
# is never enough: that stub is what a gated or cancelled pull leaves behind.
_WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth", ".ckpt")


def repo_commit(repo_id: str) -> str | None:
    """Current commit hash for a repo from refs/main, or None. Never hits the network."""
    ref = _hf_cache_dir(repo_id) / "refs" / "main"
    try:
        return ref.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def resolve_model_versions(names: Iterable[str]) -> dict[str, str]:
    """HuggingFace commit revision for each named model's repos, keyed by repo id.

    The value is the repo's refs/main commit, the id HuggingFace assigns to the
    exact snapshot in the cache. It resolves back at the source (the repo tree URL
    or HfApi().model_info(repo, revision=...)), so it is not a hash we compute.
    Best-effort provenance: the version present, not a guarantee the run loaded
    it. Unknown names and repos with no cached ref are skipped. A DPT head lists
    its backbone in hf_repos, so the head name alone covers both.
    """
    catalogue = {info.name: info for info in all_known_models()}
    versions: dict[str, str] = {}
    for name in names:
        info = catalogue.get(name)
        if info is None:
            continue
        for repo in info.hf_repos:
            commit = repo_commit(repo)
            if commit is not None:
                versions[repo] = commit
    return versions


def _snapshot_dir(repo_id: str) -> Path | None:
    """Resolve a repo's current snapshot dir from refs/main, never touching the network."""
    commit = repo_commit(repo_id)
    if commit is None:
        return None
    snap = _hf_cache_dir(repo_id) / "snapshots" / commit
    return snap if snap.is_dir() else None


# Public accessors over the module-level cache roots and per-repo helpers. Code
# outside this module (models/packs.py) must go through these rather than
# importing the globals: _HF_CACHE_ROOT / _LOGER_CKPTS are import-time snapshots
# that tests monkeypatch on the module object, and reading them at call time is
# what lets that monkeypatch (and a future HF_HOME relocation) take effect.
def hf_cache_root() -> Path:
    return _HF_CACHE_ROOT


def hf_cache_dir(repo_id: str) -> Path:
    return _hf_cache_dir(repo_id)


def snapshot_dir(repo_id: str) -> Path | None:
    return _snapshot_dir(repo_id)


def _verify_repo(repo_id: str) -> tuple[ModelStatus, str]:
    """Check one HF repo snapshot for completeness. Returns (status, reason).

    Walks the snapshot, so it is not for a repaint: the readiness rows and the
    run gate read the states `_refresh_model_status` produces on its worker
    thread instead. Deliberately not memoised -- what it decides on (a dangling
    blob symlink, a nested weights file) lives outside the snapshot directory or
    below it, so no cheap key over that directory can see a change to either.
    """
    snap = _snapshot_dir(repo_id)
    if snap is None:
        return ModelStatus.ABSENT, "not downloaded"
    return _verify_repo_uncached(snap)


def _verify_repo_uncached(snap: Path) -> tuple[ModelStatus, str]:
    entries = [p for p in snap.rglob("*") if p.is_file() or p.is_symlink()]
    # A symlink whose blob never finished downloading fails to resolve. This is
    # the fingerprint of an interrupted (not merely cancelled-before-start) pull.
    for p in entries:
        if p.is_symlink() and not p.exists():
            return ModelStatus.PARTIAL, f"incomplete data for {p.name}"

    config = snap / "config.json"
    has_config = config.exists()
    has_weights = any(p.suffix in _WEIGHT_SUFFIXES for p in entries)
    # Only README/LICENSE/.gitattributes and the like, so nothing substantive was
    # pulled (a metadata-only stub HF leaves for a gated repo, or a cancel before
    # the first real file). Treat as not-downloaded so the row prompts a plain
    # Download rather than a Repair.
    if not has_config and not has_weights:
        return ModelStatus.ABSENT, "not downloaded"

    # DPT heads ship a custom loader named in config.json#hub_inference_module
    # (coralscapes_hub_model.py). Its absence is the file that crashed at runtime.
    if has_config:
        try:
            module = json.loads(config.read_text(encoding="utf-8")).get("hub_inference_module")
        except (OSError, ValueError):
            return ModelStatus.PARTIAL, "config.json unreadable"
        if module and not (snap / module).exists():
            return ModelStatus.PARTIAL, f"missing loader {module}"

    if not has_weights:
        return ModelStatus.PARTIAL, "no weights file present"
    return ModelStatus.COMPLETE, ""


def model_status(info: ModelInfo) -> tuple[ModelStatus, str]:
    """Verified state across a model's repos and materialise targets; worst status wins."""
    worst = ModelStatus.COMPLETE
    reason = ""
    for repo in info.hf_repos:
        status, why = _verify_repo(repo)
        if status == ModelStatus.ABSENT:
            return ModelStatus.ABSENT, f"{repo}: {why}"
        if status == ModelStatus.PARTIAL and worst != ModelStatus.PARTIAL:
            worst, reason = ModelStatus.PARTIAL, f"{repo}: {why}"
    for dest in info.materialise_to.values():
        if not dest.exists():
            return ModelStatus.PARTIAL, f"missing {dest.name}"
    return worst, reason


def is_model_cached(info: ModelInfo) -> bool:
    """True only when every file needed to load the model is verified on disk."""
    return model_status(info)[0] == ModelStatus.COMPLETE



def check_hf_auth() -> tuple[str | None, bool]:
    """Return (username_or_None, can_read_gated_repos)."""
    try:
        from huggingface_hub import HfApi

        user = HfApi().whoami()
        name = user.get("name") or user.get("fullname") or "authenticated"
        auth = user.get("auth", {})
        token_info = auth.get("accessToken", {})
        fg = token_info.get("fineGrained", {})
        can_gated = fg.get("canReadGatedRepos", True)
        if token_info.get("role") != "fineGrained":
            can_gated = True
        return name, can_gated
    except Exception:
        return None, False


def check_disk_space(required_bytes: int = _MIN_FREE_BYTES) -> tuple[int, int]:
    """Return (free_bytes, required_bytes) for the HF cache mount."""
    _HF_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(_HF_CACHE_ROOT)
    return usage.free, required_bytes


def prefetch_model(info: ModelInfo, progress_cb: ProgressCallback | None = None) -> None:
    from huggingface_hub import snapshot_download

    # Field laptops fill up fast; refuse to start rather than leave a partial
    # cache that confuses is_model_cached on the next launch.
    free, required = check_disk_space()
    if free < required:
        free_gb = free / 1024**3
        required_gb = required / 1024**3
        raise InsufficientDiskSpace(
            f"Only {free_gb:.1f} GB free under {_HF_CACHE_ROOT}; "
            f"need at least {required_gb:.0f} GB to download safely."
        )

    tqdm_class = _make_silent_tqdm(progress_cb) if progress_cb is not None else None
    # Track snapshot paths so materialise_to can look up the source file for
    # each repo-relative key without re-resolving the cache layout.
    snapshot_roots: dict[str, Path] = {}
    for repo in info.hf_repos:
        logger.info("Downloading %s...", repo)
        if tqdm_class is not None:
            root = snapshot_download(repo, tqdm_class=tqdm_class)
        else:
            root = snapshot_download(repo)
        snapshot_roots[repo] = Path(root)

    _materialise_files(info, snapshot_roots)
    # The memory model sizes a model from its files. They only just arrived, so
    # whatever it concluded while they were absent is now wrong.
    from deepreefmap_gui.profiling.model_weights import forget_cached_sizes

    forget_cached_sizes()


def _materialise_files(info: ModelInfo, snapshot_roots: dict[str, Path]) -> None:
    if not info.materialise_to:
        return
    # All current materialise entries source from the first hf_repo; if a
    # future model needs files from a different repo we can extend the key
    # format to "<repo>::<path>".
    source_root = snapshot_roots[info.hf_repos[0]]
    for rel, dest in info.materialise_to.items():
        src = source_root / rel
        if not src.exists():
            raise FileNotFoundError(
                f"Expected {rel} inside {info.hf_repos[0]} snapshot at {src}"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        try:
            # Symlink keeps the HF cache the single source of truth and
            # avoids doubling disk usage on POSIX laptops.
            os.symlink(src, dest)
        except (OSError, NotImplementedError):
            shutil.copy2(src, dest)


def materialise_model(info: ModelInfo) -> None:
    """Rebuild a model's ``materialise_to`` targets from its cached snapshot.

    Runs the same copy step as ``prefetch_model`` but sourced from the already-cached
    snapshot, so an offline import can reconstitute LoGeR's fixed-path checkpoints
    without touching the network. No-op for models without ``materialise_to``.
    """
    if not info.materialise_to:
        return
    source = _snapshot_dir(info.hf_repos[0])
    if source is None:
        raise FileNotFoundError(
            f"Cannot materialise {info.name}: {info.hf_repos[0]} is not in the cache"
        )
    _materialise_files(info, {info.hf_repos[0]: source})


class _NullWriter:
    """Swallows tqdm's bar output.

    tqdm only needs write/flush from its `file`. Previously this was an open
    handle on os.devnull that nothing ever closed, so every download that passed
    a progress callback leaked a file descriptor for the life of the process.
    """

    def write(self, _text: str) -> int:
        return 0

    def flush(self) -> None:
        return None


def _make_silent_tqdm(callback: ProgressCallback) -> type:
    from tqdm.auto import tqdm as base_tqdm

    sink = _NullWriter()

    class _SignalTqdm(base_tqdm):
        def __init__(self, *args, **kwargs):
            kwargs.pop("name", None)
            kwargs["file"] = sink
            kwargs["leave"] = False
            self._track_bytes = kwargs.get("unit") == "B"
            self._last_pct = -1
            super().__init__(*args, **kwargs)

        def update(self, n=1):
            result = super().update(n)
            self._maybe_emit()
            return result

        def refresh(self, *args, **kwargs):
            result = super().refresh(*args, **kwargs)
            self._maybe_emit()
            return result

        def _maybe_emit(self) -> None:
            if not self._track_bytes or not self.total:
                return
            pct = int(100 * self.n / self.total)
            if pct != self._last_pct:
                self._last_pct = pct
                try:
                    callback(self.n, self.total)
                except DownloadCancelled:
                    raise
                except Exception:
                    logger.debug("progress callback raised", exc_info=True)

    return _SignalTqdm


def hf_login(token: str) -> str:
    from huggingface_hub import login

    login(token=token, add_to_git_credential=False)
    user, _can_gated = check_hf_auth()
    if not user:
        raise RuntimeError("Login appeared to succeed but whoami() returned no user")
    return user


def hf_logout() -> None:
    from huggingface_hub import logout

    logout()


def _installed_siblings(info: ModelInfo) -> list[ModelInfo]:
    """Catalogue entries other than `info` that are currently installed."""
    return [m for m in all_known_models() if m.name != info.name and is_model_cached(m)]


def delete_model(info: ModelInfo) -> DeletionResult:
    """Remove one model's cached files, keeping anything a sibling still needs.

    Several entries deliberately share a repo: `loger` and `loger_star` are two
    checkpoint folders inside one download, and each DINOv3 backbone is listed
    both on its own and as the encoder a DPT head fetches at first use. Deleting
    every revision of `info.hf_repos` would take an installed sibling's weights
    with it, leaving that sibling reporting PARTIAL with nothing to explain why.
    """
    from huggingface_hub import scan_cache_dir

    # Resolved before anything is removed, so this entry's own deletions cannot
    # change what the siblings report.
    siblings = _installed_siblings(info)
    kept_repos: dict[str, list[str]] = {}
    for other in siblings:
        for repo_id in info.hf_repos:
            if repo_id in other.hf_repos:
                kept_repos.setdefault(repo_id, []).append(other.name)
    sibling_destinations = {d for other in siblings for d in other.materialise_to.values()}

    # Drop materialised symlinks/copies first; the HF cache scan below will
    # leave those orphaned otherwise. Skip a destination an installed sibling
    # also claims, and one that is already gone.
    for dest in info.materialise_to.values():
        if dest in sibling_destinations:
            continue
        try:
            if dest.is_symlink() or dest.exists():
                dest.unlink()
        except FileNotFoundError:
            pass

    doomed = [r for r in info.hf_repos if r not in kept_repos]
    if not doomed:
        return DeletionResult(0, kept_repos)
    # cache_dir explicitly: a bare scan_cache_dir() reads the import-time
    # HF_HUB_CACHE constant, which is the one path in this module that would not
    # follow hf_cache_root().
    cache = scan_cache_dir(cache_dir=hf_cache_root())
    revisions: list[str] = []
    for repo in cache.repos:
        if repo.repo_id in doomed:
            revisions.extend(rev.commit_hash for rev in repo.revisions)
    if not revisions:
        return DeletionResult(0, kept_repos)
    strategy = cache.delete_revisions(*revisions)
    strategy.execute()
    return DeletionResult(len(revisions), kept_repos)
