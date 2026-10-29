"""The coverage badge generator used by CI."""

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "coverage_badge.py"
spec = importlib.util.spec_from_file_location("coverage_badge", SCRIPT)
badge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(badge)


def report(tmp_path, rate):
    path = tmp_path / "coverage.xml"
    path.write_text(f'<?xml version="1.0"?><coverage line-rate="{rate}"></coverage>')
    return path


@pytest.mark.parametrize(
    "rate, percent", [("0.9649", 96), ("0.5", 50), ("1", 100), ("0", 0), ("0.905", 90)]
)
def test_percent_is_read_and_rounded(tmp_path, rate, percent):
    assert badge.read_percent(report(tmp_path, rate)) == percent


def test_a_report_without_a_rate_is_an_error(tmp_path):
    path = tmp_path / "coverage.xml"
    path.write_text("<coverage></coverage>")
    with pytest.raises(ValueError, match="line-rate"):
        badge.read_percent(path)


@pytest.mark.parametrize(
    "percent, colour",
    [(100, "#4c1"), (95, "#4c1"), (94, "#97ca00"), (90, "#97ca00"), (89, "#a4a61d"),
     (70, "#dfb317"), (60, "#fe7d37"), (59, "#e05d44"), (0, "#e05d44")],
)  # fmt: skip
def test_colour_thresholds(percent, colour):
    assert badge.colour_for(percent) == colour


def test_render_is_valid_svg_showing_the_percentage():
    root = ET.fromstring(badge.render(96))
    assert root.tag.endswith("svg")
    assert "coverage: 96%" in root.get("aria-label")


def test_main_writes_the_file(tmp_path, capsys):
    out = tmp_path / "coverage.svg"
    assert badge.main(["x", str(report(tmp_path, "0.88")), str(out)]) == 0
    assert "88%" in out.read_text()
    assert "88%" in capsys.readouterr().out


def test_main_needs_two_arguments(capsys):
    assert badge.main(["x"]) == 2
