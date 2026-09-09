from types import SimpleNamespace

import cad_quote
import pytest


@pytest.mark.parametrize("command", ["run", "resume"])
@pytest.mark.parametrize("skip", [False, True])
def test_no_workbook_flag_reaches_pipeline(command, skip, monkeypatch):
    argv = [command, "input"]
    if command == "run":
        argv.extend(["--out", "output"])
    if skip:
        argv.append("--no-workbook")
    args = cad_quote.build_parser().parse_args(argv)
    captured = {}

    def fake_pipeline(*positional, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(status=SimpleNamespace(value="REVIEW"))

    monkeypatch.setattr(cad_quote, f"{command}_pipeline", fake_pipeline)
    monkeypatch.setattr(cad_quote, "_pipeline_cli_summary", lambda _: {})
    assert args.handler(args) == 0
    assert captured["export_workbook"] is not skip
    # Image creation and workbook creation are independent switches.
    assert captured["render_evidence"] is True
