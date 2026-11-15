# -*- coding: utf-8 -*-

"""
batch import orchestration

responsibilities split across modules:

  * identifiers.py - stable file digest and table id
  * interfaces.py  - one-transaction whole-table replacement
  * locking.py     - single cross-process writer lock
  * progress.py    - append-only, crash-tolerant progress log
  * importer.py    - this module: pre-flight checks, file scheduling,
                     per-file failure isolation, resume skipping and the
                     batch summary

all human-facing diagnostics and the summary go to stderr; one json result
line per imported table goes to stdout.
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, TextIO

from .core import df_serialize_table, get_df_from_file
from .identifiers import compute_table_id, file_digest, table_headers
from .locking import LockAcquireError, writer_lock_for
from .progress import ProgressLog

EXCEL_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".xlsb")


@dataclass
class ImportSummary:
    """
    counters describing exactly what happened in one batch
    """

    files_scanned: int = 0
    tables_found: int = 0
    tables_inserted: int = 0
    tables_updated: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    rows_written: int = 0
    progress_lines_discarded: int = 0
    failures: List[dict] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            "eparse import summary",
            f"  files scanned:      {self.files_scanned}",
            f"  tables found:       {self.tables_found}",
            f"  tables inserted:    {self.tables_inserted}",
            f"  tables updated:     {self.tables_updated}",
            f"  files skipped:      {self.files_skipped}",
            f"  files failed:       {self.files_failed}",
            f"  rows written:       {self.rows_written}",
            f"  progress lines discarded: {self.progress_lines_discarded}",
        ]
        for item in self.failures:
            lines.append(f"  FAIL {item['file']}: {item['reason']}")
        return "\n".join(lines)


def discover_excel_files(targets: Sequence, recursive: bool = False) -> List[Path]:
    """
    expand files and directories into a filename-sorted list of excel files
    """

    files: List[Path] = []
    for target in targets:
        path = Path(target)
        if path.is_dir():
            pattern = "**/*" if recursive else "*"
            for candidate in path.glob(pattern):
                if candidate.is_file() and candidate.suffix.lower() in EXCEL_SUFFIXES:
                    files.append(candidate)
        elif path.is_file() and path.suffix.lower() in EXCEL_SUFFIXES:
            # explicit targets are filtered too, so database and progress
            # files living next to the spreadsheets are never treated as
            # inputs
            files.append(path)

    # sorted by filename for deterministic scheduling
    return sorted(files, key=lambda p: str(p))


class Importer:
    """
    runs a resumable, idempotent batch import against one target database
    """

    def __init__(
        self,
        interface,
        files: Sequence[Path],
        progress_path,
        lock_timeout: float = 30.0,
        loose: bool = True,
        recursive: bool = False,
        sheet: Sequence = (),
        table: Optional[str] = None,
        exclude_nested: bool = False,
        na_tolerance_r: int = 2,
        na_tolerance_c: int = 2,
        stdout: Optional[TextIO] = None,
        stderr: Optional[TextIO] = None,
    ):
        self.interface = interface
        self.files = list(files)
        self.progress_path = progress_path
        self.lock_timeout = lock_timeout
        self.loose = loose
        self.recursive = recursive
        self.sheet = tuple(sheet)
        self.table = table
        self.exclude_nested = exclude_nested
        self.na_tolerance_r = na_tolerance_r
        self.na_tolerance_c = na_tolerance_c
        # resolve lazily at call time so test runners that replace
        # sys.stdout/sys.stderr are not bypassed by import-time binding
        self.stdout = stdout
        self.stderr = stderr
        self.summary = ImportSummary()

    @property
    def out(self) -> TextIO:
        return self.stdout if self.stdout is not None else sys.stdout

    @property
    def err(self) -> TextIO:
        return self.stderr if self.stderr is not None else sys.stderr

    # ------------------------------------------------------------------ #
    # pre-flight
    # ------------------------------------------------------------------ #
    def preflight(self) -> None:
        """
        validate every condition that should fail before any work starts:

          * progress path, if it exists, must be a regular file
          * target database must exist and be writable
          * every input file must be readable and its identity digest
            computable
        """

        ProgressLog.validate_path(self.progress_path)
        self.interface.assert_writable()

        # computing every digest up front means a table identity can never
        # fail halfway through the batch
        self._digests = {}
        for path in self.files:
            try:
                self._digests[str(path)] = file_digest(path)
            except OSError as exc:
                raise ValueError(
                    f"cannot compute table identity for {path}: {exc}"
                ) from exc

    # ------------------------------------------------------------------ #
    # main loop
    # ------------------------------------------------------------------ #
    def run(self) -> ImportSummary:
        self.summary.files_scanned = len(self.files)
        self.preflight()

        lock = writer_lock_for(self.interface, timeout=self.lock_timeout)
        try:
            lock.acquire()
        except LockAcquireError as exc:
            # fail fast / timeout: no work is done, state is untouched
            print(f"writer lock error - {exc}", file=self.err)
            raise

        try:
            progress = ProgressLog(self.progress_path)
            self.summary.progress_lines_discarded = progress.open()
            if self.summary.progress_lines_discarded:
                print(
                    f"discarded {self.summary.progress_lines_discarded} "
                    "truncated progress line(s)",
                    file=self.err,
                )

            try:
                for path in self.files:
                    self._process_file(path, progress)
            finally:
                progress.close()
        finally:
            lock.release()

        print(self.summary.render(), file=self.err)
        return self.summary

    # ------------------------------------------------------------------ #
    # per-file processing
    # ------------------------------------------------------------------ #
    def _process_file(self, path: Path, progress: ProgressLog) -> None:
        path_str = str(path)
        digest = self._digests[path_str]

        # resume: a previously successful file (same path + same content)
        # is skipped and counted separately from successes
        if progress.is_succeeded(path_str, digest):
            self.summary.files_skipped += 1
            print(f"skipped (already imported): {path_str}", file=self.err)
            return

        file_failed = False
        failure_reason: Optional[str] = None

        try:
            for table_df, anchor, _name, sheet_name in get_df_from_file(
                path,
                loose=self.loose,
                sheet=self.sheet,
                table=self.table,
                na_tolerance_r=self.na_tolerance_r,
                na_tolerance_c=self.na_tolerance_c,
                exclude_nested=self.exclude_nested,
            ):
                self.summary.tables_found += 1
                try:
                    result = self._import_table(
                        path, digest, table_df, anchor, sheet_name, progress
                    )
                except Exception as exc:  # one bad table fails its file only
                    file_failed = True
                    failure_reason = f"table at {anchor}: {exc}"
                    print(
                        f"error in {path_str} {failure_reason}",
                        file=self.err,
                    )
                    break
                if result["action"] == "inserted":
                    self.summary.tables_inserted += 1
                else:
                    self.summary.tables_updated += 1
                self.summary.rows_written += result["rows"]
        except Exception as exc:
            # file-level parse failure is isolated: later files still run
            file_failed = True
            failure_reason = str(exc)
            print(f"error reading {path_str}: {exc}", file=self.err)

        if file_failed:
            self.summary.files_failed += 1
            self.summary.failures.append(
                {"file": path_str, "reason": failure_reason or "unknown error"}
            )
            progress.log_file(path_str, digest, "failed", reason=failure_reason)
        else:
            progress.log_file(path_str, digest, "success")

    def _import_table(
        self, path, digest, table_df, anchor, sheet_name, progress: ProgressLog
    ) -> dict:
        headers = table_headers(table_df)
        table_id = compute_table_id(digest, sheet_name, anchor, headers)

        serialized = df_serialize_table(
            table_df,
            name=str(table_df.iloc[0, 0]) if table_df.size else "",
            sheet=sheet_name,
            f_name=path.name,
        )
        n_rows = table_df.shape[0]
        n_cols = table_df.shape[1]

        meta = {
            "f_name": path.name,
            "file_digest": digest,
            "sheet": str(sheet_name),
            "anchor": anchor,
            "headers": json.dumps(headers, ensure_ascii=False),
            "n_rows": n_rows,
            "n_cols": n_cols,
        }

        result = self.interface.upsert_table(table_id, serialized, meta)

        progress.log_table(
            file=str(path),
            file_hash=digest,
            table_id=table_id,
            anchor=anchor,
            sheet=str(sheet_name),
            status=result["action"],
            rows=result["rows"],
        )
        # stdout is reserved for machine-readable results
        print(
            json.dumps(
                {
                    "file": str(path),
                    "sheet": str(sheet_name),
                    "anchor": anchor,
                    "table_id": table_id,
                    "status": result["action"],
                    "rows": result["rows"],
                },
                ensure_ascii=False,
            ),
            file=self.out,
        )
        return result
