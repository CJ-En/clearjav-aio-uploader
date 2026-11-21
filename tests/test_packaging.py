"""The Windows program's icon (packaging/windows/make_icon.py) is ClearJAV's seal, the same drawing as the page's."""

import importlib.util
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "packaging" / "windows"
WEB = ROOT / "clearjav_aio" / "web"
SIZES = [16, 24, 32, 48, 64, 128, 256]


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ico_images(data: bytes) -> dict[int, bytes]:
    """The images of an .ico by size, checked to be PNGs of that size (what make_icon.py writes)."""
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1), "not an icon file"
    images = {}
    for n in range(count):
        width, height, colors, _, planes, bits, size, offset = struct.unpack("<BBBBHHII", data[6 + 16 * n:22 + 16 * n])
        image = data[offset:offset + size]
        assert len(image) == size and image[:8] == b"\x89PNG\r\n\x1a\n", f"image {n} is not a whole PNG"
        assert image[12:16] == b"IHDR"
        png_width, png_height, depth, color = struct.unpack(">IIBB", image[16:26])
        assert (png_width, png_height, depth, color) == (width or 256, height or 256, 8, 6), f"image {n}: {png_width}"
        assert (colors, planes, bits) == (0, 1, 32)
        assert zlib.crc32(image[12:29]) & 0xFFFFFFFF == struct.unpack(">I", image[29:33])[0], f"image {n}: IHDR checksum"
        images[png_width] = image
    return images


def pixels(image: bytes) -> bytes:
    """The RGBA pixels of a PNG make_icon.py wrote (one IDAT, filter "none" on every row)."""
    width = struct.unpack(">I", image[16:20])[0]
    start = image.index(b"IDAT")
    length = struct.unpack(">I", image[start - 4:start])[0]
    raw = zlib.decompress(image[start + 4:start + 4 + length])
    stride = width * 4 + 1
    assert len(raw) == stride * width and all(raw[row * stride] == 0 for row in range(width))
    return b"".join(raw[row * stride + 1:(row + 1) * stride] for row in range(width))


class IconTest(unittest.TestCase):
    """cj.ico (the program's icon): the seal in every size Windows asks for."""

    def setUp(self):
        self.make_icon = load(HERE / "make_icon.py")

    def test_the_icon_in_the_repository(self):
        self.assertEqual(sorted(ico_images((ROOT / "cj.ico").read_bytes())), SIZES)
        self.assertIn('icon=str(ROOT / "cj.ico")', (HERE / "clearjav-uploader.spec").read_text(encoding="utf-8"))

    def test_make_icon_draws_the_seal(self):
        folder = Path(tempfile.mkdtemp(prefix="aio icon "))
        self.addCleanup(shutil.rmtree, folder, True)
        images = ico_images(self.make_icon.make(folder / "test.ico").read_bytes())
        self.assertEqual(sorted(images), SIZES)
        # The icon in git is make_icon.py's (the same pixels; another zlib may pack them differently).
        kept = ico_images((ROOT / "cj.ico").read_bytes())
        for size in SIZES:
            self.assertEqual(pixels(images[size]), pixels(kept[size]), size)

        def pixel(size, x, y):
            at = (y * size + x) * 4
            return pixels(images[size])[at:at + 4]

        tile, ink = bytes([*self.make_icon.TILE, 255]), bytes([*self.make_icon.INK, 255])
        self.assertEqual(tile, bytes([0x5E, 0xC4, 0xFA, 255]))  # the brand's blue, the page's Dark accent
        self.assertEqual(ink, bytes([0x04, 0x12, 0x1F, 255]))
        # 16 px, seal-small: the tile's right edge, the C's upright, a round corner that is partly see-through.
        self.assertEqual(pixel(16, 15, 8), tile)
        self.assertEqual(pixel(16, 2, 8), ink)
        self.assertEqual(pixel(16, 12, 8), ink)  # the J's upright
        self.assertTrue(0 < pixel(16, 0, 0)[3] < 255)
        # 256 px, the framed seal: tile, frame, tile between frame and letter, the C; outside the round corner.
        self.assertEqual(pixel(256, 4, 128), tile)
        self.assertEqual(pixel(256, 14, 128), ink)
        self.assertEqual(pixel(256, 30, 128), tile)
        self.assertEqual(pixel(256, 50, 128), ink)
        self.assertEqual(pixel(256, 128, 128), tile)  # between C and J
        self.assertEqual(pixel(256, 0, 0)[3], 0)

    def test_page_and_icon_draw_the_same_seal(self):
        small = self.make_icon.SMALL
        favicon = (WEB / "favicon.svg").read_text(encoding="utf-8")
        self.assertIn(f'<path d="{small["tile"]}" fill="#5ec4fa"/>', favicon)
        self.assertIn(f'<path d="{small["ink"]}" fill="#04121f"/>', favicon)
        page = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertIn('<link rel="icon" href="/favicon.svg" type="image/svg+xml">', page)
        self.assertIn(f'<path class="logo-fill" d="{small["tile"]}"/><path class="logo-ink" d="{small["ink"]}"/>', page)  # top bar
        self.assertNotIn(">CJ<", page)  # the old text mark

    def test_paths_with_other_commands_are_refused(self):
        self.assertEqual(len(self.make_icon.polygons("M0 0L4 0L4 4ZM1 1L2 1L2 2Z", 1)), 2)
        circle = self.make_icon.polygons("M0 5A5 5 0 0 1 10 5A5 5 0 0 1 0 5Z", 0.5)[0]
        self.assertTrue(all(abs(((x - 5) ** 2 + (y - 5) ** 2) ** 0.5 - 5) < 1e-9 for x, y in circle))
        self.assertGreater(len(circle), 40)
        for path in ("M0 0l4 0Z", "M0 0C1 1 2 2 3 3Z", "M0 0L4"):
            with self.assertRaises(ValueError, msg=path):
                self.make_icon.polygons(path, 1)


if __name__ == "__main__":
    unittest.main()
