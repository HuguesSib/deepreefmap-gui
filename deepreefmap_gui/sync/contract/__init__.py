"""The registry's published contract artefact, and the constants derived from it.

``sync-contract.json`` is the registry's own ``contract/sync-contract.json``,
copied in whole and never edited here. Refresh it with::

    cp ../deepreefmap-api/contract/sync-contract.json deepreefmap_gui/sync/contract/

Every version number and section name this app speaks comes out of that file, so
there is one place a contract change lands and no hand-typed integer that can
drift from what the registry published.

Nothing is imported here beyond the standard library. ``sync/client.py`` depends
on this module and on nothing else in the package, which is what keeps the client
free of the survey layer.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

ARTEFACT = "sync-contract.json"


def _document() -> dict[str, Any]:
    """The vendored artefact, read from package data so a wheel carries it."""
    raw = resources.files(__name__).joinpath(ARTEFACT).read_text(encoding="utf-8")
    return json.loads(raw)


DOCUMENT: dict[str, Any] = _document()

# The newest contract this build reads, from the artefact, and the oldest, which
# is this build's own: the registry's floor rising must not stop a laptop from
# talking to a registry that has not.
CONTRACT_VERSION: int = int(DOCUMENT["contract_version"])
MIN_CONTRACT_VERSION: int = 1
SERVER_MIN_CONTRACT_VERSION: int = int(DOCUMENT["min_contract_version"])

# Every section the contract names, in the order a document presents them.
SECTIONS: tuple[str, ...] = tuple(DOCUMENT["sections"])

# The sections a device is served on a pull. The rest are upload only, so asking
# for them would widen the cursor's meaning for rows that never come down.
PULL_SECTIONS: tuple[str, ...] = tuple(DOCUMENT["pull_sections"])

# The sections a device is served its own rows of, from OWN_ROWS_SINCE: what the
# console curated, validated or deleted of what this laptop uploaded.
OWN_ROWS_SECTIONS: tuple[str, ...] = tuple(DOCUMENT["own_rows_sections"])
OWN_ROWS_SINCE: int = int(DOCUMENT["own_rows_since"])

# Every section a pull may carry to this build.
READ_SECTIONS: tuple[str, ...] = PULL_SECTIONS + OWN_ROWS_SECTIONS

# The sections a device may author. It sends the others as ancestors of rows it
# does author, and the registry reads those rather than writing them.
PUSH_SECTIONS: tuple[str, ...] = tuple(DOCUMENT["push_sections"])

# What the contract header carries. A bare version would read as a point range.
CONTRACT_RANGE = f"{MIN_CONTRACT_VERSION}-{CONTRACT_VERSION}"

# How a connect code announces its format. The prefix is the only human-legible part
# of the pasted string, so the dialog shows it and the decoder matches on it; the
# family and version are what let an older build say a code is newer than it is.
CONNECT_CODE_FAMILY: str = str(DOCUMENT["connect_code"]["family"])
CONNECT_CODE_VERSION: int = int(DOCUMENT["connect_code"]["version"])
CONNECT_CODE_PREFIX: str = str(DOCUMENT["connect_code"]["prefix"])


def required_columns(section: str) -> tuple[str, ...]:
    """The columns the registry will not accept a row of this section without."""
    for table in DOCUMENT["tables"]:
        if table["section"] == section:
            return tuple(
                column["name"] for column in table["columns"] if not column["nullable"]
            )
    raise KeyError(f"{section!r} is not a section the contract names")
