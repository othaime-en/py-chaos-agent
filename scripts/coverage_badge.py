#!/usr/bin/env python3
"""
Write an SVG coverage badge from a coverage.xml report.

    python scripts/coverage_badge.py coverage.xml coverage.svg

No dependencies and no third-party service. CI runs this on every push to main
and publishes the result to the `badges` branch, which the README links to.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# (minimum percent, colour), best first
COLOURS = [
    (95, "#4c1"),  # bright green
    (90, "#97ca00"),  # green
    (80, "#a4a61d"),  # yellow-green
    (70, "#dfb317"),  # yellow
    (60, "#fe7d37"),  # orange
    (0, "#e05d44"),  # red
]


def read_percent(report: Path) -> int:
    """Total line coverage of a coverage.py XML report, as a whole percent."""
    rate = ET.parse(report).getroot().get("line-rate")
    if rate is None:
        raise ValueError(f"{report} has no line-rate attribute")
    return int(round(float(rate) * 100))


def colour_for(percent: int) -> str:
    for minimum, colour in COLOURS:
        if percent >= minimum:
            return colour
    return COLOURS[-1][1]


def render(percent: int) -> str:
    label, value = "coverage", f"{percent}%"
    label_w = 6 * len(label) + 10
    value_w = 7 * len(value) + 10
    total = label_w + value_w
    colour = colour_for(percent)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img" aria-label="{label}: {value}">
  <title>{label}: {value}</title>
  <linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/><stop offset="1" stop-opacity=".1"/></linearGradient>
  <clipPath id="r"><rect width="{total}" height="20" rx="3" fill="#fff"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="{label_w}" height="20" fill="#555"/>
    <rect x="{label_w}" width="{value_w}" height="20" fill="{colour}"/>
    <rect width="{total}" height="20" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
    <text x="{label_w / 2}" y="14">{label}</text>
    <text x="{label_w + value_w / 2}" y="14">{value}</text>
  </g>
</svg>
"""


def main(argv: list) -> int:
    if len(argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    report, output = Path(argv[1]), Path(argv[2])
    percent = read_percent(report)
    output.write_text(render(percent))
    print(f"coverage {percent}% -> {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
