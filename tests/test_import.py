# -*- coding: utf-8 -*-

"""
offline unit tests for the resumable, idempotent import pipeline

every test uses temporary directories and a temporary sqlite database, so
nothing depends on external services or shared state.
"""

import io
import json

import pytest
from openpyxl import Workbook

from eparse.identifiers import (
    compute_table_id,
    file_digest,
    table_headers,
)
from eparse.importer import Importer, discover_excel_files
from eparse.interfaces import (
    ExcelParse,
    ExcelTable,
    Sqlite3Interface,
    i_factory,
)
from eparse.locking import FileWriterLock, LockAcquireError
from eparse.progress import ProgressLog


# --------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------- #
def write_xlsx(path, headers=("ID", "Name"), rows=((1, "a"), (2, "b")), start=(3, 3)):
    wb = Workbook()
    ws = wb.active
    ws.title = "DATA"
    sr, sc = start
    for j, h in enumerate(headers):
        ws.cell(row=sr, column=sc + j, value=h)
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            ws.cell(row=sr + 1 + i, column=sc + j, value=value)
    wb.save(path)


def make_importer(tmp_path, files=None, progress_name="progress.jsonl", **kwargs):
    db = tmp_path / "target.db"
    interface = i_factory(f"sqlite3:///{db}", ExcelParse)
    progress = tmp_path / progress_name
    files = files or discover_excel_files([tmp_path])
    options = dict(
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        na_tolerance_r=2,
        na_tolerance_c=2,
    )
    options.update(kwargs)
    return Importer(interface, files, progress, **options), db, progress


def cell_count(table_id):
    return ExcelParse.select().where(ExcelParse.table_id == table_id).count()


# --------------------------------------------------------------------- #
# identifiers
# --------------------------------------------------------------------- #
def test_identifier_is_stable_and_externally_reproducible(tmp_path):
    f = tmp_path / "a.xlsx"
    write_xlsx(f)

    digest = file_digest(f)
    assert isinstance(digest, str) and len(digest) == 64
    assert file_digest(f) == digest  # deterministic over unchanged bytes

    import hashlib

    payload = json.dumps(
        {
            "file_digest": digest,
            "sheet": "DATA",
            "anchor": "C3",
            "headers": ["ID", "Name"],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    table_id = compute_table_id(digest, "DATA", "C3", ["ID", "Name"])
    assert table_id == expected


def test_header_change_changes_identity():
    digest = "a" * 64
    base = compute_table_id(digest, "DATA", "C3", ["ID", "Name"])
    renamed = compute_table_id(digest, "DATA", "C3", ["ID", "Renamed"])
    moved = compute_table_id(digest, "DATA", "D4", ["ID", "Name"])
    other_sheet = compute_table_id(digest, "OTHER", "C3", ["ID", "Name"])
    assert len({base, renamed, moved, other_sheet}) == 4


def test_table_headers_normalizes_missing():
    import pandas as pd

    df = pd.DataFrame([["A", None], [1, 2]])
    assert table_headers(df) == ["A", None]


# --------------------------------------------------------------------- #
# empty directory
# --------------------------------------------------------------------- #
def test_import_empty_directory(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    importer, db, progress = make_importer(
        tmp_path, files=discover_excel_files([empty])
    )
    summary = importer.run()

    assert summary.files_scanned == 0
    assert summary.tables_found == 0
    assert summary.rows_written == 0
    assert summary.files_failed == 0
    assert progress.exists()
    assert importer.stdout.getvalue() == ""
    assert "eparse import summary" in importer.stderr.getvalue()


# --------------------------------------------------------------------- #
# re-import keeps row count identical
# --------------------------------------------------------------------- #
def test_same_file_imported_twice_does_not_grow(tmp_path):
    write_xlsx(tmp_path / "a.xlsx")
    importer, db, progress = make_importer(tmp_path)
    first = importer.run()

    assert first.tables_inserted == 1
    assert first.tables_updated == 0
    table_id = ExcelTable.get().table_id
    assert cell_count(table_id) == 6

    # fresh progress log: the table already exists -> update, not insert
    second, _, _ = make_importer(tmp_path, progress_name="progress2.jsonl")
    result = second.run()

    assert result.tables_inserted == 0
    assert result.tables_updated == 1
    assert cell_count(table_id) == 6  # no row growth, no leftover rows
    assert ExcelTable.select().count() == 1
    assert ExcelParse.select().count() == 6


# --------------------------------------------------------------------- #
# header change is a new table, old one retained
# --------------------------------------------------------------------- #
def test_changed_header_creates_new_table_and_keeps_old(tmp_path):
    f = tmp_path / "a.xlsx"
    write_xlsx(f)
    importer, _, _ = make_importer(tmp_path)
    importer.run()
    old_id = ExcelTable.get().table_id
    assert cell_count(old_id) == 6

    # same position and file, different header -> new identity
    write_xlsx(f, headers=("ID", "Renamed"))
    rerun, _, progress2 = make_importer(
        tmp_path,
        files=[f],
        progress_name="progress2.jsonl",
    )
    summary = rerun.run()

    assert summary.tables_inserted == 1
    new_id = ExcelTable.select().where(ExcelTable.table_id != old_id).get().table_id
    assert new_id != old_id
    # old table retained, new table populated
    assert cell_count(old_id) == 6
    assert cell_count(new_id) == 6
    assert ExcelTable.select().count() == 2


# --------------------------------------------------------------------- #
# whole-table replacement rolls back on failure
# --------------------------------------------------------------------- #
def test_replacement_failure_rolls_back_entirely(tmp_path, monkeypatch):
    write_xlsx(tmp_path / "a.xlsx")
    importer, _, _ = make_importer(tmp_path)
    importer.run()
    table_id = ExcelTable.get().table_id
    assert cell_count(table_id) == 6

    # force every delete to fail inside the replacement transaction
    def boom(*args, **kwargs):
        raise RuntimeError("simulated replacement failure")

    monkeypatch.setattr(ExcelParse, "delete", boom)

    with pytest.raises(RuntimeError):
        importer.interface.upsert_table(
            table_id,
            list(ExcelParse.select().dicts()),
            {
                "f_name": "a.xlsx",
                "file_digest": "b" * 64,
                "sheet": "DATA",
                "anchor": "C3",
                "headers": "[]",
                "n_rows": 3,
                "n_cols": 2,
            },
        )

    # no half-old / half-new state: original rows still present and intact
    assert cell_count(table_id) == 6
    assert ExcelTable.get(ExcelTable.table_id == table_id).n_cells == 6


# --------------------------------------------------------------------- #
# single file failure isolation
# --------------------------------------------------------------------- #
def test_one_bad_file_does_not_stop_the_batch(tmp_path):
    write_xlsx(tmp_path / "good.xlsx")
    (tmp_path / "broken.xlsx").write_bytes(b"this is not an excel workbook")

    importer, _, _ = make_importer(tmp_path)
    summary = importer.run()

    assert summary.files_scanned == 2
    assert summary.files_failed == 1
    assert summary.tables_inserted == 1
    assert summary.failures[0]["file"].endswith("broken.xlsx")
    assert summary.failures[0]["reason"]
    # good file's data still committed
    assert ExcelParse.select().count() == 6


# --------------------------------------------------------------------- #
# resume: completed files skipped, failed ones retried
# --------------------------------------------------------------------- #
def test_resume_skips_successful_and_retries_failed(tmp_path):
    good = tmp_path / "good.xlsx"
    broken = tmp_path / "broken.xlsx"
    write_xlsx(good)
    broken.write_bytes(b"not a workbook")

    first, db, progress = make_importer(tmp_path)
    s1 = first.run()
    assert s1.files_failed == 1 and s1.files_skipped == 0

    second, _, _ = make_importer(tmp_path)
    s2 = second.run()
    # good file skipped (counted separately), broken retried and failed again
    assert s2.files_skipped == 1
    assert s2.files_failed == 1
    assert s2.tables_found == 0
    assert "skipped (already imported)" in second.stderr.getvalue()

    # repair the broken file: only it gets processed now
    write_xlsx(broken, headers=("ID", "Other"))
    third, _, _ = make_importer(tmp_path)
    s3 = third.run()
    assert s3.files_skipped == 1
    assert s3.files_failed == 0
    assert s3.tables_inserted == 1


def test_changed_file_content_is_not_skipped(tmp_path):
    f = tmp_path / "a.xlsx"
    write_xlsx(f)
    importer, _, progress = make_importer(tmp_path)
    importer.run()

    # same path, different bytes -> different digest -> not a skip
    write_xlsx(f, rows=((1, "a"), (2, "b"), (3, "c")))
    rerun, _, _ = make_importer(tmp_path)
    summary = rerun.run()
    assert summary.files_skipped == 0
    assert summary.files_failed == 0


# --------------------------------------------------------------------- #
# truncated progress line tolerance
# --------------------------------------------------------------------- #
def test_truncated_progress_line_is_discarded(tmp_path):
    progress = tmp_path / "progress.jsonl"
    log = ProgressLog(progress)
    assert log.open() == 0
    log.log_file("f1", "d" * 64, "success")
    log.log_file("f2", "e" * 64, "success")
    log.close()

    raw = progress.read_bytes()
    progress.write_bytes(raw[:-4])  # chop the final line mid-record

    recovered = ProgressLog(progress)
    discarded = recovered.open()
    assert discarded == 1
    # earlier complete records survived and remain usable
    assert recovered.is_succeeded("f1", "d" * 64)
    assert not recovered.is_succeeded("f2", "e" * 64)
    recovered.close()

    # a second open finds nothing left to discard
    assert ProgressLog(progress).open() == 0


def test_progress_path_that_is_a_directory_fails(tmp_path):
    with pytest.raises(ValueError):
        ProgressLog(tmp_path).open()


# --------------------------------------------------------------------- #
# two writers contending for one database lock
# --------------------------------------------------------------------- #
def test_second_writer_is_rejected_fail_fast(tmp_path):
    lock_path = tmp_path / "target.db.lock"
    holder = FileWriterLock(lock_path, timeout=0)
    holder.acquire()

    contender = FileWriterLock(lock_path, timeout=0)
    with pytest.raises(LockAcquireError):
        contender.acquire()

    holder.release()
    # lock available again immediately
    with FileWriterLock(lock_path, timeout=0):
        pass


def test_importer_serializes_two_writers(tmp_path):
    write_xlsx(tmp_path / "a.xlsx")
    importer, db, progress = make_importer(tmp_path, lock_timeout=0)
    lock_path = tmp_path / "target.db.lock"

    with FileWriterLock(lock_path, timeout=0):
        # another importer against the same database must fail fast rather
        # than interleave writes
        with pytest.raises(LockAcquireError):
            importer.run()

    # once the lock frees, the import runs normally
    summary = importer.run()
    assert summary.files_failed == 0
    assert ExcelParse.select().count() == 6


# --------------------------------------------------------------------- #
# pre-flight failures happen before processing
# --------------------------------------------------------------------- #
def test_unwritable_database_fails_preflight(tmp_path, monkeypatch):
    write_xlsx(tmp_path / "a.xlsx")
    importer, _, _ = make_importer(tmp_path)

    def boom(self):
        raise OSError("database is not writable")

    monkeypatch.setattr(Sqlite3Interface, "assert_writable", boom)
    with pytest.raises(OSError):
        importer.run()
    # nothing was processed
    assert importer.summary.files_scanned == 1
    assert not (tmp_path / "progress.jsonl").exists()


def test_uncomputable_identity_fails_preflight(tmp_path, monkeypatch):
    write_xlsx(tmp_path / "a.xlsx")
    importer, _, _ = make_importer(tmp_path)

    def boom(path):
        raise OSError("cannot read file")

    monkeypatch.setattr("eparse.importer.file_digest", boom)
    with pytest.raises(ValueError):
        importer.run()


# --------------------------------------------------------------------- #
# summary accounting
# --------------------------------------------------------------------- #
def test_summary_counts_are_consistent(tmp_path):
    write_xlsx(tmp_path / "a.xlsx")
    write_xlsx(tmp_path / "b.xlsx", headers=("K", "V"))
    (tmp_path / "c_broken.xlsx").write_bytes(b"bad")
    (tmp_path / "notes.txt").write_text("not an excel file")

    importer, _, _ = make_importer(tmp_path)
    summary = importer.run()

    assert summary.files_scanned == 3  # .txt filtered out, broken counted
    assert summary.tables_found == 2
    assert summary.tables_inserted == 2
    assert summary.tables_updated == 0
    assert summary.files_skipped == 0
    assert summary.files_failed == 1
    assert summary.rows_written == 12  # 2 tables x 6 cells each
    assert len(summary.failures) == 1

    rendered = summary.render()
    for label in (
        "files scanned",
        "tables found",
        "tables inserted",
        "tables updated",
        "files skipped",
        "files failed",
        "rows written",
    ):
        assert label in rendered

    # stdout carries only machine-readable table results, no summary text
    out_lines = [ln for ln in importer.stdout.getvalue().splitlines() if ln]
    assert len(out_lines) == 2
    for line in out_lines:
        record = json.loads(line)
        assert "table_id" in record and record["status"] == "inserted"
    assert "eparse import summary" not in importer.stdout.getvalue()
    # diagnostics and summary live on stderr only
    assert "eparse import summary" in importer.stderr.getvalue()
