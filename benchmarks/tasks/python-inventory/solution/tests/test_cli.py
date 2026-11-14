from inventory.cli import main


def f_(tmp_path):
    return str(tmp_path / "inv.json")


def test_add_and_list(tmp_path, capsys):
    f = f_(tmp_path)
    assert main(["--file", f, "add", "B2", "Bolt", "10"]) == 0
    assert main(["--file", f, "add", "A1", "Anchor", "3"]) == 0
    capsys.readouterr()
    assert main(["--file", f, "list"]) == 0
    assert capsys.readouterr().out.splitlines() == ["A1\tAnchor\t3\t3", "B2\tBolt\t10\t10"]


def test_restock_unknown_sku_is_an_error(tmp_path, capsys):
    assert main(["--file", f_(tmp_path), "restock", "X", "1"]) == 2
    assert "unknown SKU X" in capsys.readouterr().err


def test_reserve_prints_id(tmp_path, capsys):
    f = f_(tmp_path)
    main(["--file", f, "add", "A1", "Anchor", "3"])
    assert main(["--file", f, "reserve", "A1", "1", "ann"]) == 0
    assert capsys.readouterr().out.strip() == "R1"


def test_reserve_too_many(tmp_path, capsys):
    f = f_(tmp_path)
    main(["--file", f, "add", "A1", "Anchor", "3"])
    assert main(["--file", f, "reserve", "A1", "4", "ann"]) == 2


def test_release(tmp_path, capsys):
    f = f_(tmp_path)
    main(["--file", f, "add", "A1", "Anchor", "3"])
    main(["--file", f, "reserve", "A1", "1", "ann"])
    assert main(["--file", f, "release", "R1"]) == 0


def test_report_header(tmp_path, capsys):
    f = f_(tmp_path)
    main(["--file", f, "add", "A1", "Anchor", "3"])
    capsys.readouterr()
    main(["--file", f, "report"])
    assert capsys.readouterr().out.splitlines()[0] == "sku\tstock\treserved\tavailable"
