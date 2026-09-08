import argparse
import json

import cad_quote
import cadquote.route_context
import pytest


@pytest.mark.parametrize("incomplete", [False, True])
def test_native_context_failure_is_not_successful_empty_scan(
    tmp_path, monkeypatch, capsys, incomplete
):
    args = argparse.Namespace(
        index=tmp_path / "index.json",
        panels=tmp_path / "panels.json",
        out=tmp_path / "out.json",
        review_out=None,
        refresh_native_frames=True,
        probe_native=False,
        group_images_dir=None,
    )
    monkeypatch.setattr(cad_quote, "_load_index", lambda _: ([], []))
    monkeypatch.setattr(cad_quote, "_apply_panels", lambda *args: ([], []))
    monkeypatch.setattr(cad_quote, "_load_json", lambda _: {})
    monkeypatch.setattr(
        cadquote.route_context,
        "refresh_native_route_frames",
        lambda *args: (
            [],
            {
                "incomplete": incomplete,
                "issues": [{"reason": "PermissionError"}] if incomplete else [],
            },
        ),
    )
    assert cad_quote.command_detail_routes(args) == (2 if incomplete else 0)
    result = json.loads(args.out.read_text(encoding="utf8"))
    assert result["incomplete"] is incomplete
    assert result["node_view_groups"]["groups"] == []
    printed = json.loads(capsys.readouterr().out)
    assert printed["state"] == ("REVIEW_INCOMPLETE" if incomplete else "REVIEW_ONLY")


def test_cli_images_require_native_probe():
    with pytest.raises(ValueError, match="probe-native"):
        cad_quote.command_detail_routes(
            argparse.Namespace(group_images_dir="images", probe_native=False)
        )
