"""Makes the Windows program's icon (cj.ico at the repository's root) from ClearJAV's seal, with Python alone.

The drawings are ClearJAV's logo (docs/brand in the site's repository), blue: a #5ec4fa tile with #04121f letters.
As the brand's README says, `seal-small` (block letters on a 16 grid, edges on whole pixels) is used from 16 to 48 px,
and the framed `seal` (512 grid) from 64 px up. The paths are SVG paths of M, L, A (circular arcs) and Z; the arcs are
cut into short straight pieces. Each size is drawn with supersampling (several samples per pixel, so the edges are
smooth), stored as a PNG, and the PNGs are packed into one .ico (Windows Vista and later read PNG images in icons).

    python packaging/windows/make_icon.py [output.ico]
"""

from __future__ import annotations

import math
import re
import struct
import sys
import zlib
from pathlib import Path

SIZES = (16, 24, 32, 48, 64, 128, 256)
TILE = (0x5E, 0xC4, 0xFA)  # brand "blue": ClearJAV Dark's accent
INK = (0x04, 0x12, 0x1F)  # the letters on it
SMALL_UP_TO = 48  # seal-small up to here, the framed seal above

# seal-small-blue.svg, on a 16 grid: the same paths as the page's favicon.svg and its top bar. The letters are blocks
# that overlap, so they are filled with the nonzero rule.
SMALL = {
    "grid": 16,
    "tile": "M2 0L14 0A2 2 0 0 1 16 2L16 14A2 2 0 0 1 14 16L2 16A2 2 0 0 1 0 14L0 2A2 2 0 0 1 2 0Z",
    "ink": "M2 2L7 2L7 4L2 4ZM5 2L7 2L7 5L5 5ZM2 2L4 2L4 14L2 14ZM2 12L7 12L7 14L2 14ZM5 11L7 11L7 14L5 14Z"
           "M12 2L14 2L14 14L12 14ZM9 12L14 12L14 14L9 14ZM9 10L11 10L11 14L9 14Z",
    "rule": "nonzero",
}
# seal-blue.svg, on a 512 grid: the tile, then the frame (its outer and inner outline: even-odd leaves the ring) and
# the letters C and J inside it.
LARGE = {
    "grid": 512,
    "tile": "M56 0L456 0A56 56 0 0 1 512 56L512 456A56 56 0 0 1 456 512L56 512A56 56 0 0 1 0 456L0 56A56 56 0 0 1 56 0Z",
    "ink": "M64 24L448 24A40 40 0 0 1 488 64L488 448A40 40 0 0 1 448 488L64 488A40 40 0 0 1 24 448L24 64A40 40 0 0 1 64 24Z"
           "M66 40A26 26 0 0 0 40 66L40 446A26 26 0 0 0 66 472L446 472A26 26 0 0 0 472 446L472 66A26 26 0 0 0 446 40Z"
           "M190 174L190 154L132 154L132 358L190 358L190 338L240 338L240 369A39 39 0 0 1 201 408L121 408A39 39 0 0 1 82 369"
           "L82 143A39 39 0 0 1 121 104L201 104A39 39 0 0 1 240 143L240 174Z"
           "M430 104L430 369A39 39 0 0 1 391 408L311 408A39 39 0 0 1 272 369L272 338L322 338L322 358L380 358L380 104Z",
    "rule": "evenodd",
}
OUTPUT = Path(__file__).resolve().parents[2] / "cj.ico"


# -- paths ------------------------------------------------------------------------------------------


def _arc(start, rx, ry, rotation, large, sweep, end, step):
    """Points along an SVG arc from `start` to `end` (the endpoint form of the SVG spec, appendix F.6.5), at most
    `step` grid units apart. The last point is exactly `end`."""
    (x1, y1), (x2, y2) = start, end
    if (x1, y1) == (x2, y2):
        return []
    rx, ry = abs(rx), abs(ry)
    if not rx or not ry:
        return [end]
    phi = math.radians(rotation)
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cos_p * dx + sin_p * dy, -sin_p * dx + cos_p * dy
    scale = x1p ** 2 / rx ** 2 + y1p ** 2 / ry ** 2
    if scale > 1:  # radii too small for the two points: grown as the spec says
        rx, ry = rx * math.sqrt(scale), ry * math.sqrt(scale)
    numerator = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    denominator = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    factor = math.sqrt(max(0.0, numerator / denominator))
    if large == sweep:
        factor = -factor
    cxp, cyp = factor * rx * y1p / ry, -factor * ry * x1p / rx
    cx = cos_p * cxp - sin_p * cyp + (x1 + x2) / 2
    cy = sin_p * cxp + cos_p * cyp + (y1 + y2) / 2

    def angle(ux, uy, vx, vy):
        return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)

    theta = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    delta = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi
    count = max(2, math.ceil(abs(delta) * max(rx, ry) / step))
    points = []
    for n in range(1, count):
        t = theta + delta * n / count
        points.append((cx + rx * math.cos(t) * cos_p - ry * math.sin(t) * sin_p,
                       cy + rx * math.cos(t) * sin_p + ry * math.sin(t) * cos_p))
    return points + [end]


def polygons(path: str, step: float) -> list[list[tuple[float, float]]]:
    """The outlines of an SVG path made of absolute M, L, A and Z, arcs cut into pieces at most `step` long."""
    tokens = re.findall(r"[A-Za-z]|-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", path)
    shapes: list[list[tuple[float, float]]] = []
    at = 0
    command = ""
    current = start = (0.0, 0.0)

    def numbers(count):
        nonlocal at
        values = tokens[at:at + count]
        if len(values) != count or any(re.fullmatch(r"[A-Za-z]", v) for v in values):
            raise ValueError(f"too few numbers near {' '.join(tokens[max(0, at - 1):at + count])}")
        at += count
        return [float(v) for v in values]

    while at < len(tokens):
        if re.fullmatch(r"[A-Za-z]", tokens[at]):
            command = tokens[at]
            at += 1
            if command not in "MLAZ":
                raise ValueError(f"only absolute M, L, A and Z are drawn, not {command}")
        if command == "Z":
            current = start
            command = ""
            continue
        if command == "M":
            current = start = tuple(numbers(2))
            shapes.append([current])
            command = "L"  # more pairs after M are lines (SVG)
        elif command == "L":
            current = tuple(numbers(2))
            shapes[-1].append(current)
        elif command == "A":
            rx, ry, rotation, large, sweep, x, y = numbers(7)
            shapes[-1].extend(_arc(current, rx, ry, rotation, bool(large), bool(sweep), (x, y), step))
            current = (x, y)
        else:
            raise ValueError(f"unexpected {tokens[at]!r} in the path")
    return [shape for shape in shapes if len(shape) > 2]


# -- drawing ---------------------------------------------------------------------------------------


def _edges(shapes):
    edges = []
    for shape in shapes:
        for (x0, y0), (x1, y1) in zip(shape, shape[1:] + shape[:1]):
            if y0 != y1:
                edges.append((x0, y0, x1, y1))
    return edges


def _spans(edges, y: float, rule: str):
    """The inside of the shapes along the line at height `y`: (from x, to x) pairs."""
    crossings = []
    for x0, y0, x1, y1 in edges:
        if min(y0, y1) <= y < max(y0, y1):
            crossings.append((x0 + (y - y0) * (x1 - x0) / (y1 - y0), 1 if y1 > y0 else -1))
    crossings.sort()
    spans = []
    if rule == "evenodd":
        for (a, _), (b, _) in zip(crossings[::2], crossings[1::2]):
            spans.append((a, b))
        return spans
    winding, begin = 0, 0.0
    for x, direction in crossings:
        if winding == 0:
            begin = x
        winding += direction
        if winding == 0:
            spans.append((begin, x))
    return spans


def render(size: int) -> bytes:
    """RGBA pixels, rows top to bottom (non-premultiplied, as PNG stores them)."""
    seal = SMALL if size <= SMALL_UP_TO else LARGE
    grid = seal["grid"]
    samples = 8 if size <= 64 else 4  # per side of a pixel
    columns = size * samples
    unit = grid / columns  # grid units per sample
    tile = _edges(polygons(seal["tile"], grid / 256))
    ink = _edges(polygons(seal["ink"], grid / 256))

    def row_of(edges, y, rule):
        row = bytearray(columns)
        for a, b in _spans(edges, y, rule):
            first, last = max(0, math.ceil(a / unit - 0.5)), min(columns, math.ceil(b / unit - 0.5))
            if last > first:
                row[first:last] = b"\1" * (last - first)
        return row

    out = bytearray()
    for py in range(size):
        tile_count, ink_count = [0] * size, [0] * size
        for sy in range(samples):
            y = (py * samples + sy + 0.5) * unit
            tile_row = row_of(tile, y, "nonzero")
            # Ink only where the tile is (it always is: the letters and frame lie inside it).
            ink_row = (int.from_bytes(row_of(ink, y, seal["rule"]), "little") & int.from_bytes(tile_row, "little")).to_bytes(columns, "little")
            for px in range(size):
                cut = slice(px * samples, (px + 1) * samples)
                tile_count[px] += sum(tile_row[cut])
                ink_count[px] += sum(ink_row[cut])
        for t, i in zip(tile_count, ink_count):
            if not t:
                out += b"\0\0\0\0"
                continue
            # Ink over the tile: the color is their mix inside the tile, the tile's coverage is the alpha.
            out += bytes(round(((t - i) * a + i * b) / t) for a, b in zip(TILE, INK))
            out.append(round(255 * t / samples ** 2))
    return bytes(out)


def png(size: int, rgba: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    stride = size * 4
    raw = b"".join(b"\0" + rgba[row * stride:(row + 1) * stride] for row in range(size))  # filter "none" per row
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8 bits, RGBA, no interlace
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def ico(images: dict[int, bytes]) -> bytes:
    """An .ico holding one PNG per size (width and height 0 mean 256)."""
    entries, offset = b"", 6 + 16 * len(images)
    for size, data in images.items():
        side = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return struct.pack("<HHH", 0, 1, len(images)) + entries + b"".join(images.values())


def make(path: Path = OUTPUT, sizes=SIZES) -> Path:
    path.write_bytes(ico({size: png(size, render(size)) for size in sizes}))
    return path


if __name__ == "__main__":
    print(make(Path(sys.argv[1]) if len(sys.argv) > 1 else OUTPUT))
