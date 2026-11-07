"""DataRescue's deterministic, local data-quality engine."""

from __future__ import annotations

from .core import (
    MAX_BYTES,
    MAX_COLUMNS,
    MAX_ROWS,
    ColumnRule,
    DataError,
    JsonNumber,
    Options,
    Schema,
    convert,
    parse,
    scan,
)
from .export import bundle

__version__ = "0.1.0"

__all__ = [
    "MAX_BYTES",
    "MAX_COLUMNS",
    "MAX_ROWS",
    "ColumnRule",
    "DataError",
    "JsonNumber",
    "Options",
    "Schema",
    "__version__",
    "bundle",
    "convert",
    "parse",
    "scan",
]
