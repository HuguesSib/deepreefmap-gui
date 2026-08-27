"""Renaming a pass, from wherever a pass is shown.

A pass's name is the one piece of a device's work a person chooses, and it
reaches the registry: ``label`` is a pushed column, so the console shows what
was typed here. It used to be reachable from the cart alone, which is the page
about queueing work rather than the pages about looking at it.
"""

from __future__ import annotations

import uuid

from PySide6.QtWidgets import QInputDialog, QWidget

from deepreefmap_gui.survey.labels import taken_labels, unique_label
from deepreefmap_gui.survey.store import SurveyStore

RENAME_ACTION = "Rename pass…"


def renamed_note(label: str) -> str:
    """What to say where the row that was renamed does not show a name."""
    return f"This pass is now called {label!r}." if label else "This pass is back to its derived name."
_PROMPT = "What this pass is called. Clear it for the derived name."


def rename_pass(
    parent: QWidget, store: SurveyStore, pass_id: uuid.UUID, *, shown: str | None = None
) -> str | None:
    """Ask what this pass should be called and store the answer.

    Returns a sentence for the status bar when the wanted name was already
    taken, and None otherwise -- including when the dialog was cancelled, which
    changes nothing and so has nothing to say.

    An emptied field is a request for the derived name back, not for a nameless
    pass: the generated text is never stored, because storing it would freeze
    today's generator into every old row.
    """
    pass_ = store.get_pass(pass_id)
    if pass_ is None:
        return None
    # `shown` is what the caller's row says, where a row says anything: the cart
    # names every pass, the clip's pass rows name the transect instead. Without
    # one the field opens on the stored name, empty where nobody has set one.
    typed, accepted = QInputDialog.getText(
        parent, "Rename pass", _PROMPT, text=pass_.label if shown is None else shown
    )
    if not accepted:
        return None
    wanted = typed.strip()
    if not wanted:
        pass_.label = ""
        store.update_pass(pass_)
        return None
    pass_.label = unique_label(wanted, taken_labels(store.list_passes(), exclude=pass_id))
    store.update_pass(pass_)
    if pass_.label != wanted:
        return f"Another pass is already called {wanted!r}; this one is {pass_.label!r}."
    return None
