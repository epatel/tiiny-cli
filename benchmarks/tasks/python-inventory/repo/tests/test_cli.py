from inventory.cli import main


def test_add_and_list(tmp_path, capsys):
    f = str(tmp_path / "inv.json")
    assert main(["--file", f, "add", "B2", "Bolt", "10"]) == 0
    assert main(["--file", f, "add", "A1", "Anchor", "3"]) == 0
    capsys.readouterr()
    assert main(["--file", f, "list"]) == 0
    assert capsys.readouterr().out.splitlines() == ["A1\tAnchor\t3", "B2\tBolt\t10"]


def test_restock_unknown_sku_is_an_error(tmp_path, capsys):
    f = str(tmp_path / "inv.json")
    assert main(["--file", f, "restock", "X", "1"]) == 2
    assert "unknown SKU X" in capsys.readouterr().err
