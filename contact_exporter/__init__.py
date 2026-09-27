"""WeChat contact exporter — local, read-only CSV/TXT export tool.

Additive module over pyweixin; does not modify upstream core logic.
Only reads contacts via the public API Contacts.get_friends_detail() and
writes local files. No messaging, no mutation, no hooks, no DB access.
"""

from __future__ import annotations

import sys
from pathlib import Path

# In a source checkout the WeChat packages live under ``src``. Keep the
# repository's documented ``python -m contact_exporter...`` entry points
# runnable without requiring an editable install first.
_PROJECT_SRC = Path(__file__).resolve().parents[1] / "src"
if _PROJECT_SRC.is_dir() and str(_PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(_PROJECT_SRC))

__version__ = "0.1.0"

from .models import Contact, EXPORT_FIELDS  # noqa: E402
from .normalizer import normalize  # noqa: E402
from .exporter import export_csv, export_txt, export_all  # noqa: E402

__all__ = [
    "Contact",
    "EXPORT_FIELDS",
    "normalize",
    "export_csv",
    "export_txt",
    "export_all",
]
