"""CLI subcommand tests: new / list / export / render / datasets, plus main()
dispatch (--version, bare invocation -> serve). All run against throwaway
CANVAS_DIR/OUT_DIR/DATA_DIR (cli_dirs + data_dir fixtures), never the real
~/.figfoundry. cli binds CANVAS_DIR/OUT_DIR at import time, so both are patched."""
import argparse
import json
import os
import sys

import pytest

from figfoundry import __version__, canvas, cli


@pytest.fixture
def cli_dirs(tmp_path, monkeypatch):
    canvases = tmp_path / "canvases"
    canvases.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(cli, "CANVAS_DIR", str(canvases))
    monkeypatch.setattr(cli, "OUT_DIR", str(out))
    return canvases, out


def _line_spec():
    spec = canvas.blank_spec()
    spec["dataset"] = "sample.npz"
    spec["plots"] = [canvas._new_plot("p0", [0.1, 0.15, 0.8, 0.7], "@x", "@y")]
    return spec


def _make_canvas(canvases, name, spec):
    slug = "20260101-000000-deadbeef"
    with open(os.path.join(str(canvases), f"{slug}.json"), "w") as f:
        json.dump({"name": name, "spec": spec, "updated": 0.0}, f)
    return slug


# ------------------------------------------------------------------ new/list
def test_cli_new_then_list(cli_dirs, capsys):
    canvases, _ = cli_dirs
    cli.cmd_list(argparse.Namespace())
    assert capsys.readouterr().out.strip() == "no figures yet"
    cli.cmd_new(argparse.Namespace(name="My Fig"))
    slug = capsys.readouterr().out.strip()
    assert slug  # the slug is the only stdout
    with open(os.path.join(str(canvases), f"{slug}.json")) as f:
        doc = json.load(f)
    assert doc["name"] == "My Fig" and doc["spec"]["plots"] == []
    cli.cmd_list(argparse.Namespace())
    out = capsys.readouterr().out
    assert slug in out and "My Fig" in out and "(0 plots)" in out


def test_cli_new_default_name(cli_dirs, capsys):
    cli.cmd_new(argparse.Namespace(name=None))
    slug = capsys.readouterr().out.strip()
    with open(os.path.join(cli_dirs[0], f"{slug}.json")) as f:
        doc = json.load(f)
    assert doc["name"] == "Untitled"


# ------------------------------------------------------------------- export
def test_cli_export_default_and_custom_out(cli_dirs, data_dir, capsys):
    canvases, out = cli_dirs
    slug = _make_canvas(canvases, "My Fig", _line_spec())
    cli.cmd_export(argparse.Namespace(slug=slug, out=None))
    assert str(out / "My_Fig.py") in capsys.readouterr().out  # spaces -> underscores
    with open(out / "My_Fig.py", encoding="utf-8") as f:
        code = f.read()
    assert "def build():" in code and "sample.npz" in code
    custom = str(out / "sub" / "c.py")
    cli.cmd_export(argparse.Namespace(slug=slug, out=custom))
    assert os.path.exists(custom)  # --out path is created, including parent dirs


def test_cli_export_missing_canvas(cli_dirs):
    with pytest.raises(SystemExit):
        cli.cmd_export(argparse.Namespace(slug="nope", out=None))


# ------------------------------------------------------------------- render
def test_cli_render_formats(cli_dirs, data_dir):
    canvases, out = cli_dirs
    slug = _make_canvas(canvases, "Fig", _line_spec())
    for fmt, magic in [("png", b"\x89PNG\r\n\x1a\n"), ("svg", b"<?xml"), ("pdf", b"%PDF")]:
        cli.cmd_render(argparse.Namespace(slug=slug, format=fmt, out=None))
        path = out / f"Fig.{fmt}"
        assert path.exists(), fmt
        assert path.read_bytes()[: len(magic)] == magic


def test_cli_render_custom_out(cli_dirs, data_dir):
    canvases, out = cli_dirs
    slug = _make_canvas(canvases, "Fig", _line_spec())
    target = str(out / "custom" / "render.svg")
    cli.cmd_render(argparse.Namespace(slug=slug, format="svg", out=target))
    with open(target, "rb") as f:
        assert f.read(5) == b"<?xml"


# ----------------------------------------------------------------- datasets
def test_cli_datasets(cli_dirs, data_dir, capsys):
    cli.cmd_datasets(argparse.Namespace())
    out = capsys.readouterr().out
    assert "sample.npz" in out and "(npz, 7 signals)" in out


# ------------------------------------------------------------- main() dispatch
def test_main_version(cli_dirs, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["figfoundry", "--version"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0
    assert f"figfoundry {__version__}" in capsys.readouterr().out


def test_main_bare_invocation_serves(cli_dirs, data_dir, monkeypatch):
    calls = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.update(kw))
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: calls.setdefault("browser", url))
    monkeypatch.setattr(sys, "argv", ["figfoundry"])
    cli.main()
    assert calls["port"] == 8037 and calls["host"] == "127.0.0.1"
    assert calls["browser"] == "http://127.0.0.1:8037"  # browser opens by default


def test_main_serve_flags(cli_dirs, data_dir, monkeypatch):
    calls = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.update(kw))
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: calls.setdefault("browser", url))
    monkeypatch.setattr(sys, "argv", ["figfoundry", "serve", "--port", "9123", "--no-browser"])
    cli.main()
    assert calls["port"] == 9123
    assert "browser" not in calls  # --no-browser suppresses webbrowser.open
