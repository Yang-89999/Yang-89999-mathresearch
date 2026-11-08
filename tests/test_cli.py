from mathresearch.cli import main


def test_init_status_and_charter(tmp_path, capsys):
    assert main(["--dir", str(tmp_path), "init"]) == 0
    assert (tmp_path / "charter.yaml").exists()
    assert (tmp_path / "research.db").exists()

    assert main(["--dir", str(tmp_path), "status"]) == 0
    out = capsys.readouterr().out
    assert "questions" in out and "engines on PATH" in out

    assert main(["--dir", str(tmp_path), "charter", "show"]) == 0
    out = capsys.readouterr().out
    assert "# Research charter" in out


def test_run_script_records_experiment(tmp_path, capsys):
    main(["--dir", str(tmp_path), "init"])
    script = tmp_path / "s.py"
    script.write_text("print('RESULT: {\"ok\": true}')\n", encoding="utf-8")
    assert main(["--dir", str(tmp_path), "run-script", str(script), "--engine", "python"]) == 0
    err = capsys.readouterr().err
    assert "recorded as exp_" in err

    from mathresearch import db
    conn = db.connect(tmp_path / "research.db")
    assert db.table_counts(conn)["experiments"] == 1
