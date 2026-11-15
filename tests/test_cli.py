# -*- coding: utf-8 -*-

"""
unit tests for eparse cli
"""

import pytest
from click.testing import CliRunner
from openpyxl import Workbook

from eparse.cli import main

kwargs = {"obj": {}, "catch_exceptions": False}


def _write_xlsx(path):
    wb = Workbook()
    ws = wb.active
    ws.cell(row=3, column=3, value="ID")
    ws.cell(row=3, column=4, value="Name")
    ws.cell(row=4, column=3, value=1)
    ws.cell(row=4, column=4, value="a")
    wb.save(path)


def test_main():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_scan():
    runner = CliRunner()
    result = runner.invoke(main, ["-f", "tests/", "scan"], **kwargs)
    assert result.exit_code == 0
    assert "eparse_unit_test_data" in result.output


def test_parse():
    runner = CliRunner()
    result = runner.invoke(
        main, ["-f", "tests/", "parse", "--exclude-nested"], **kwargs
    )
    assert result.exit_code == 0
    assert "eparse_unit_test_data" in result.output


def test_query():
    runner = CliRunner()
    result = runner.invoke(main, ["-i", "sqlite3:///tests/test.db", "query"], **kwargs)
    assert result.exit_code == 0
    assert result.output == ""


def test_migrate():
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["-i", "sqlite3:///tests/test.db", "migrate", "-m", "migration_000102_000200"],
        **kwargs,
    )
    assert result.exit_code == 1
    assert "duplicate column name: timestamp" in result.output


def test_outputs():
    runner = CliRunner()
    result = runner.invoke(main, ["-o", "null:///", "scan"], **kwargs)
    assert result.exit_code == 0
    assert result.output == ""
    result = runner.invoke(main, ["-o", "stdout:///", "scan"], **kwargs)
    assert result.exit_code == 0
    assert result.output == ""
    result = runner.invoke(main, ["-o", "sqlite3:///:memory:", "scan"], **kwargs)
    assert result.exit_code == 0
    assert result.output == ""
    result = runner.invoke(main, ["-o", "test", "scan"], **kwargs)
    assert result.exit_code == 1
    assert "test is not a recognized endpoint" in result.output
    with pytest.raises(ValueError):
        result = runner.invoke(main, ["-d", "-o", "test", "scan"], **kwargs)


def test_import_command_end_to_end(tmp_path):
    _write_xlsx(tmp_path / "a.xlsx")
    db = tmp_path / "target.db"
    progress = tmp_path / "progress.jsonl"
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "-o",
            f"sqlite3:///{db}",
            "-f",
            str(tmp_path),
            "import",
            "-p",
            str(progress),
        ],
        **kwargs,
    )
    assert result.exit_code == 0
    # machine-readable results on stdout, summary on stderr
    assert "table_id" in result.stdout
    assert "eparse import summary" in result.stderr
    assert "eparse import summary" not in result.stdout

    # rerun: successful file is skipped and stdout stays empty
    rerun = runner.invoke(
        main,
        [
            "-o",
            f"sqlite3:///{db}",
            "-f",
            str(tmp_path),
            "import",
            "-p",
            str(progress),
        ],
        **kwargs,
    )
    assert rerun.exit_code == 0
    assert rerun.stdout == ""
    assert "files skipped:      1" in rerun.stderr


def test_import_preflight_errors(tmp_path):
    _write_xlsx(tmp_path / "a.xlsx")
    db = tmp_path / "target.db"
    runner = CliRunner()

    # progress path exists but is a directory
    result = runner.invoke(
        main,
        [
            "-o",
            f"sqlite3:///{db}",
            "-f",
            str(tmp_path),
            "import",
            "-p",
            str(tmp_path),
        ],
        **kwargs,
    )
    assert result.exit_code == 1
    assert "not a file" in result.stderr
    assert result.stdout == ""

    # non-database output is rejected
    result = runner.invoke(
        main,
        [
            "-o",
            "null:///",
            "-f",
            str(tmp_path),
            "import",
            "-p",
            str(tmp_path / "p.jsonl"),
        ],
        **kwargs,
    )
    assert result.exit_code == 1
    assert "requires a database" in result.stderr


def test_import_lock_contention_fails_fast(tmp_path):
    from eparse.locking import FileWriterLock

    _write_xlsx(tmp_path / "a.xlsx")
    db = tmp_path / "target.db"
    lock = FileWriterLock(tmp_path / "target.db.lock", timeout=0)
    lock.acquire()
    try:
        result = CliRunner().invoke(
            main,
            [
                "-o",
                f"sqlite3:///{db}",
                "-f",
                str(tmp_path),
                "import",
                "-p",
                str(tmp_path / "p.jsonl"),
                "--lock-timeout",
                "0",
            ],
            **kwargs,
        )
        assert result.exit_code == 1
        assert "writer" in result.stderr.lower()
    finally:
        lock.release()
