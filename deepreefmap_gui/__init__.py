"""DeepReefMap desktop application: plan a reef survey, process the day's videos, browse the results.

`cli.py::main` takes an optional run directory and calls `app.py::launch`, which prepares the Qt
platform and builds one window. One `QApplication`, one window, no second process: everything
below hangs off `app.py::DeepReefMapWindow`.

The reconstruction itself is not here. It lives in the `deepreefmap` library, called in-process
from `simple/batch.py` (`runs/loading.py` only reads cached runs back). This package is the
interface around it, plus everything the library has no opinion about: which runs exist, which
transect they belong to, whether this laptop can run another one.

## Where the code for a feature is

The window is a single class fused from 21 mixins, so a feature is a file rather than a widget
subtree. `class <Mixin>` is the only grep you need after this table:

| What you see                                          | Mixin                  | File                   |
| ----------------------------------------------------- | ---------------------- | ---------------------- |
| Transects: the lines, the map, imports                | `SimplePlanMixin`      | `simple/plan.py`       |
| Cart: the queued sections, and the batch as it runs   | `SimpleBatchMixin`     | `simple/batch.py`      |
| Repeat-pass comparison, shown under Transects         | `SimpleAnalysisMixin`  | `simple/analysis.py`   |
| Videos: the clip library, its sections and link state | `VideoLibraryMixin`    | `runs/videos.py`       |
| Browse: the run archive, its rail, table and detail   | `BrowseMixin`          | `runs/browse.py`       |
| The shell: destinations, settings dialog, the store   | `InterfaceShellMixin`  | `simple/mode.py`       |
| Setup: the destination and its header button          | `SimpleMachineMixin`   | `simple/machine.py`    |
| The notification bell, its list and the mute book     | `NotificationCenterMixin` | `notify/center_ui.py` |
| Readiness rows: graphics card, models, disk           | `SimpleSetupMixin`     | `simple/setup.py`      |
| System gauges and the no-video benchmark              | `SystemPanelMixin`     | `system/system_tab.py` |
| Model status, download, delete, HuggingFace login     | `ModelManagementMixin` | `models/cache_ui.py`   |
| Model packs: export, import, reveal the cache         | `ModelLibraryMixin`    | `models/packs_ui.py`   |
| The run settings form and the bottom status strip     | `FormPanelMixin`       | `form/panel.py`        |
| Progress bars, the ETA, the status line               | `ProgressBarsMixin`    | `runs/progress.py`     |
| Opening a cached run, pause and stop                  | `RunLoadingMixin`      | `runs/loading.py`      |
| The run banner and the workspace reset                | `PastRunsMixin`        | `runs/past_runs.py`    |
| Results: ortho preview, transect crop, exports, cover | `ResultsMixin`         | `runs/results.py`      |
| Viewer controls: playback, legend, picking, app mode  | `ViewerControlsMixin`  | `viewer/controls.py`   |
| Update check and install, the desktop entry toggle    | `VersionCheckMixin`    | `update/version.py`    |
| Storage: one drive's runs, clips and what can go      | `StorageMixin`         | `storage/page.py`      |
| Server: the registry, the device, and metadata sync   | `ServerPageMixin`      | `server/page_ui.py`    |

Widgets those mixins build but do not own are their own modules beside them (`runs/run_table.py`,
`runs/run_detail.py`, `viewer/legend.py`, and so on). Each subpackage's `__init__.py` states its
contract in two lines; read that before adding a file to it.

## The mixin contract

The mixins share one `self`. None of them has an `__init__`: state appears when whichever
`_build_*` method needs it first runs, so readers go through `getattr` and timers are created
lazily. Three things hold the arrangement together:

- `core/window_protocol.py` types it. `MixinBase` declares every attribute and method one mixin
  assigns and another reads, so mypy can resolve a cross-file `self._foo`. `TYPE_CHECKING`-only:
  at runtime `MixinBase is object`, and PySide6 refuses a second `QObject` base, so the signals
  are restated there rather than inherited.
- `tests/core/test_window_protocol_sync.py` keeps it honest by AST comparison. An attribute
  assigned in one mixin and read in another, but undeclared, fails it.
- `tests/gui/test_no_mixin_shadowing.py` forbids two mixins defining the same method name. The
  MRO picks one and silently discards the rest.

Cross-thread work goes through the `_sig_*` signals declared on `DeepReefMapWindow`; see `app.py`
for what connects to what. `QTimer.singleShot` from a worker thread does nothing at all.

## Qt-free modules and their Qt layer

`x.py` holds logic with no Qt import; `x_ui.py` puts it on screen. `models/` follows it
literally: `packs.py` is the model-pack format and the file copying, `packs_ui.py` the dialogs
and the mixin that drive them. Prefer the suffix for new splits.

`notify/` follows it too: `model.py`, `conditions.py`, `center.py` and `log.py` are pure, and the
`_ui` modules are the bell, the popover and the Activity view. So does `server/`: `state.py` and
`enrolment.py` read and act, `page_ui.py` and `connect_ui.py` are the page and the dialog. `sync/`
below it is Qt-free throughout, and knows nothing about a window.

The same split exists under older names, and those stay: `survey/` is the Qt-free domain layer
under `simple/`'s UI, `simple/section_state.py` the pure verdict behind the header badges,
`models/cache.py` the pure side of `models/cache_ui.py`. `io/`, `packaging/`, `profiling/`,
`cover.py` and `paths.py` are pure throughout.

The point is not purity. A pure module is tested in `tests/<package>/` in milliseconds, where a
Qt one needs `tests/gui/` and a real window.

## Names for the same thing

Several features answer to two or three names. Prefer the first in prose, comments and UI text.

- **Destination**, not workspace, step or tab. There are four (Transects, Videos, Cart, Browse)
  and none gates another. `DESTINATIONS` in `simple/mode.py` is the list; the Cart pill's section
  key is still `process` in code. Setup and View are sections of the same stack but not
  destinations, so `SIMPLE_SECTIONS` is the longer tuple.
- **Survey**, not simple. `simple/` is the package name left over from the Simple/Advanced
  interface split. `survey/` is a different thing: the Qt-free domain layer (transects, passes,
  the store, the preset) that `simple/` draws.
- **Browse**, not data. The destination, the file and the mixin all say Browse; only the widget
  attributes are still `_data_*`. Nothing user-facing says "data".
- **Setup**, for the destination holding everything about this computer: whether it can process a
  dive, which models are on it, what it is doing while it runs. Its first view is **Readiness**.
  The file is `simple/setup.py`; the mixin is `SimpleMachineMixin` in `simple/machine.py`, the
  label's old name.
- **Pass**, not clip, video or section, for a cutout of a video, in code and in the UI
  alike. The README's glossary defines the whole vocabulary (pass, run, session, cart,
  order); here is how it maps to code. A run's `batch_id` names its session, the pass's own `batch_id` only its
  origin. The **cart** is `SurveyStore.current_cart` (the newest un-started session) and
  membership is the `batch_item` table, so one pass can be ordered in many sessions. The
  session's class is still `SurveyBatch` and its column `survey_batch`: the schema name is
  load-bearing for `rebuild_from_scan`, so only the UI says session.
- **Run** is only ever a finished output. The destination that produces one is the Cart, so the
  word never names a queue.
- **Condition** and **event**, for the two things the notification centre carries. A condition is
  derived from live state and resolves itself, so it is reconciled and gets one row per episode;
  an event happened once and is appended. **Fingerprint** is what joins an episode to itself and
  what a reader silences: it comes from `SectionState.cause`, never from the sentence, so
  rewording a message does not reopen it or void anybody's mute. **Clearing** puts one occurrence
  away, **silencing** ("never show this again") puts the whole class away and lives in QSettings
  rather than the survey.

## Things that would otherwise bite

- **The run form is never on screen.** `form/panel.py` builds it into a hidden holder and lends
  it to the run settings dialog. `_collect_run_settings()` reads exactly those widgets, so a
  setting with no widget there cannot reach a run.
- **Panels are lent, not rebuilt.** Browse, the model library, the system panel and the results
  panel are each one widget re-parented into whichever destination shows them. Two copies would
  disagree the moment a download or a path edit landed against only one.
- **Adding a shared attribute is a two-file change.** The mixin that assigns it, and
  `core/window_protocol.py`. Skip the second and the sync test fails; a `hasattr` guard instead
  hides the gap from mypy entirely.
- **Direction has one spelling.** `survey/models/transect_pass.py` owns the vocabulary
  (`direction_arrow`, `direction_text`), `core/widgets.py::DIRECTION_COLORS` is where it meets the
  theme, and `core/icons.py::direction_arrow_icon` pairs the glyph with the ink. Never a bare
  "forward" in a label: the arrow is what carries direction on a selected row, where neither
  colour clears AA contrast.
- **Read `tests/README.md` before writing a test here.** The `tests/gui/` fixtures build a whole
  window, so a pure test placed there costs seconds per test for nothing.
"""
