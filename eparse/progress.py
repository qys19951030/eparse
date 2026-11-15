# -*- coding: utf-8 -*-

"""
append-only batch progress log (newline-delimited json / jsonl)

the log is written one json object per line, flushed after every line, so a
crash at any moment leaves every previously written line complete and
parseable.  on recovery the file is read in chunks and a truncated final
line (or any unparseable tail line) is discarded by truncating the file back
to the byte offset of the last good line; the number of discarded lines is
reported.  the log is never read back wholesale for rewriting.

record kinds:

  {"kind": "table", "file": <path str>, "file_digest": <hex str>,
   "table_id": <hex str>, "anchor": <rc str>, "sheet": <str>,
   "status": "inserted" | "updated", "rows": <int>}

  {"kind": "file", "file": <path str>, "file_digest": <hex str>,
   "status": "success" | "failed", "reason": <str or null>}

a file counts as fully imported only when a "file" record with status
"success" and a matching file_digest exists.
"""

import json
import os
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

READ_CHUNK = 64 * 1024


class ProgressLog:
    """
    append-only jsonl progress log with truncation-tolerant recovery
    """

    def __init__(self, path):
        self.path = str(path)
        self._fh = None
        # files (path, digest) that have a success record
        self._succeeded: Set[Tuple[str, str]] = set()

    @staticmethod
    def validate_path(path) -> None:
        """
        pre-flight check: an existing progress path must be a regular file
        """

        p = Path(path)
        if p.exists() and not p.is_file():
            raise ValueError(
                f"progress path exists but is not a file: {path}"
            )

    def open(self) -> int:
        """
        open (or create) the log, recover it, and return the number of
        discarded (truncated / unparseable) tail lines
        """

        self.validate_path(self.path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        discarded = self._recover()
        # newline buffering keeps json lines atomic; flush after each write
        self._fh = open(self.path, "a+", encoding="utf-8", newline="\n")
        return discarded

    def _recover(self) -> int:
        """
        stream the file in chunks, loading good records; the first bad
        complete line (or a truncated final line) ends the valid region,
        which is then truncated away.  return the number of dropped lines.
        """

        path = Path(self.path)
        if not path.exists() or path.stat().st_size == 0:
            return 0

        self._succeeded.clear()
        good_offset = 0
        carry = ""
        truncated = False

        with open(self.path, "r", encoding="utf-8") as fh:
            while True:
                chunk = fh.read(READ_CHUNK)
                if not chunk:
                    # any leftover text without a newline is a truncated line
                    if carry:
                        truncated = True
                    break
                carry += chunk
                lines = carry.split("\n")
                carry = lines.pop()
                for line in lines:
                    rec = self._parse_line(line)
                    if rec is None:
                        truncated = True
                        break
                    self._load(rec)
                    good_offset += len(line.encode("utf-8")) + 1
                if truncated:
                    break

        if not truncated:
            return 0

        # count every remaining line (bad or partial) as discarded
        with open(self.path, "rb") as fh:
            fh.seek(good_offset)
            tail = fh.read().decode("utf-8")
        discarded = len([ln for ln in tail.splitlines() if ln != ""])

        with open(self.path, "r+b") as fh:
            fh.truncate(good_offset)

        return discarded

    @staticmethod
    def _parse_line(line: str) -> Optional[Dict]:
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(rec, dict) or "kind" not in rec:
            return None
        return rec

    def _load(self, rec: Dict) -> None:
        if rec.get("kind") == "file" and rec.get("status") == "success":
            self._succeeded.add((rec.get("file"), rec.get("file_digest")))

    def append(self, record: Dict) -> None:
        """
        append a single json record as one complete line and flush it
        """

        if self._fh is None:
            raise RuntimeError("progress log is not open")
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        self._fh.write(line + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._load(record)

    def log_table(
        self,
        file: str,
        file_hash: str,
        table_id: str,
        anchor: str,
        sheet: str,
        status: str,
        rows: int,
    ) -> None:
        self.append(
            {
                "kind": "table",
                "file": file,
                "file_digest": file_hash,
                "table_id": table_id,
                "anchor": anchor,
                "sheet": sheet,
                "status": status,
                "rows": rows,
            }
        )

    def log_file(
        self, file: str, file_hash: str, status: str, reason: Optional[str] = None
    ) -> None:
        self.append(
            {
                "kind": "file",
                "file": file,
                "file_digest": file_hash,
                "status": status,
                "reason": reason,
            }
        )

    def is_succeeded(self, file: str, file_hash: str) -> bool:
        return (file, file_hash) in self._succeeded

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
