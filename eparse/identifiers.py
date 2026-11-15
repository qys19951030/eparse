# -*- coding: utf-8 -*-

"""
stable identifiers for imported excel tables

a table's identity is fully determined by four externally reproducible
ingredients:

  1. file_digest - sha256 hex digest of the raw file bytes
  2. sheet       - worksheet name
  3. anchor      - excel-style top-left cell reference, e.g. "C3"
  4. headers     - normalized first row of the parsed table

the table id is::

    sha256(
        json.dumps(
            {
                "file_digest": <hex str>,
                "sheet": <str>,
                "anchor": <str>,
                "headers": [<str or null>, ...],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()

any change to the headers therefore produces a new table identity while the
old table is retained; re-importing unchanged content at the same position
yields the same id and is treated as an update.
"""

import hashlib
import json
from typing import List, Optional

import pandas as pd

CHUNK_SIZE = 64 * 1024


def file_digest(path, chunk_size: int = CHUNK_SIZE) -> str:
    """
    compute the sha256 hex digest of a file without loading it into memory

    raises OSError if the file cannot be read
    """

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _normalize_cell(value) -> Optional[str]:
    """
    normalize a header cell to a plain string (None for missing values)
    """

    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        # unwrap numpy scalars before stringifying
        value = value.item()
    return str(value)


def table_headers(df: pd.DataFrame) -> List[Optional[str]]:
    """
    extract the normalized header row (first row) of a parsed table
    """

    if df.shape[0] == 0:
        return []
    return [_normalize_cell(v) for v in df.iloc[0].tolist()]


def compute_table_id(
    file_hash: str,
    sheet: str,
    anchor: str,
    headers: List[Optional[str]],
) -> str:
    """
    compute the stable, externally reproducible id of a table
    """

    payload = json.dumps(
        {
            "file_digest": file_hash,
            "sheet": str(sheet),
            "anchor": str(anchor),
            "headers": list(headers),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
