"""Audit workpaper: one run as a DOCX or PDF document (spec docs/design/phase-5-workpaper.md).

`build_document` makes the document model; `render_docx` and `render_pdf` (which need
the optional extra `workpaper`) write it. Importing this package needs neither.
"""

from audit_workbench.workpaper.build import (
    DEFAULT_TITLE,
    SampleMismatchError,
    build_document,
    resolve_run_date,
    verify_sample,
)
from audit_workbench.workpaper.model import Document

__all__ = [
    "DEFAULT_TITLE",
    "Document",
    "SampleMismatchError",
    "build_document",
    "resolve_run_date",
    "verify_sample",
]
