"""Image bounding before upload.

DESIGN-V2 §13.8 proposed downscaling on the premise that phone photos are 2-4 MB.
Measured 2026-08-23, LINE already delivers at most ~1.64 MP / ~250 KB average, so
that premise did not hold for this system. What remains worth doing is a BOUND
for pathological input (a full-resolution photo through the CLI path) and a
smaller copy for the Flex hero, which LINE displays small.

Deliberately conservative for the Gemini copy: the model still read both
packaging labels exactly right (186 kcal / 40g protein, and 330 kcal) all the way
down to 768px, but two photos are not enough evidence to risk a dense fine-print
nutrition table for a saving that is meaningless at ~15-30 calls a day.
"""

import io
import unittest

from coach import images


def _jpeg(width, height):
    from PIL import Image
    buf = io.BytesIO()
    # Noise, not flat colour: a flat image compresses so well that a resize can
    # come out LARGER, which the function then correctly refuses to use.
    im = Image.effect_noise((width, height), 60).convert("RGB")
    im.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def _size(raw):
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as im:
        return im.size


class Downscale(unittest.TestCase):
    def test_oversized_image_is_bounded(self):
        out = images.downscale(_jpeg(4000, 3000), 1568)
        self.assertEqual(max(_size(out)), 1568)

    def test_aspect_ratio_is_preserved(self):
        out = images.downscale(_jpeg(4000, 2000), 1000)
        width, height = _size(out)
        self.assertEqual(max(width, height), 1000)
        self.assertAlmostEqual(width / height, 2.0, places=1)

    def test_image_within_the_bound_is_returned_untouched(self):
        # An ordinary LINE photo must not be re-encoded for nothing.
        raw = _jpeg(1108, 1477)
        self.assertIs(images.downscale(raw, 1568), raw)

    def test_never_upscales(self):
        raw = _jpeg(400, 300)
        self.assertIs(images.downscale(raw, 2000), raw)

    def test_result_is_smaller_than_the_original(self):
        raw = _jpeg(3000, 3000)
        self.assertLess(len(images.downscale(raw, 800)), len(raw))


class GracefulFallback(unittest.TestCase):
    """A photo that cannot be resized must still be logged."""

    def test_non_image_bytes_are_returned_unchanged(self):
        self.assertEqual(images.downscale(b"not an image", 1024), b"not an image")

    def test_empty_bytes_are_returned_unchanged(self):
        self.assertEqual(images.downscale(b"", 1024), b"")

    def test_missing_pillow_returns_the_original(self):
        import builtins
        real_import = builtins.__import__

        def no_pillow(name, *args, **kwargs):
            if name.startswith("PIL"):
                raise ImportError("no PIL")
            return real_import(name, *args, **kwargs)

        raw = _jpeg(4000, 3000)          # built while PIL is still importable
        builtins.__import__ = no_pillow
        self.addCleanup(setattr, builtins, "__import__", real_import)
        self.assertIs(images.downscale(raw, 800), raw)


class Bounds(unittest.TestCase):
    def test_hero_copy_is_smaller_than_the_vision_copy(self):
        # The card shows it small and LINE fetches it over the tunnel; the
        # model's copy is the one that must stay legible.
        self.assertLess(images.HERO_MAX_EDGE, images.VISION_MAX_EDGE)

    def test_ordinary_line_photos_are_barely_touched(self):
        # Largest observed from LINE: 1108x1477 (passes through untouched, see
        # test_image_within_the_bound_is_returned_untouched) and 960x1706, which
        # is bounded to 882x1568 — an 8% trim, nowhere near the point where
        # label legibility came into question.
        tallest_line_edge = 1706
        self.assertGreater(images.VISION_MAX_EDGE / tallest_line_edge, 0.9)

    def test_vision_bound_stays_well_clear_of_the_legibility_floor(self):
        # Labels still read correctly at 768px; the bound keeps a wide margin
        # over that rather than chasing the token saving.
        self.assertGreaterEqual(images.VISION_MAX_EDGE, 1024)


class SaveTempImage(unittest.TestCase):
    def test_stored_hero_is_bounded(self):
        token = images.save_temp_image(_jpeg(3000, 2000), "image/jpeg")
        path = images.resolve_temp_image(token)
        self.addCleanup(path.unlink, missing_ok=True)
        self.assertIsNotNone(path)
        self.assertEqual(max(_size(path.read_bytes())), images.HERO_MAX_EDGE)


if __name__ == "__main__":
    unittest.main()
