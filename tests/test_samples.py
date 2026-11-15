"""Regression test on the real samples in samples/: every packet must decode cleanly, to the same frames as before.

The expected hashes come from this decoder, whose output on the full original clips was checked frame by frame
by eye and against an earlier version. A changed hash means the decoder's output changed.
Run with:  python3 -m unittest discover tests
"""
import hashlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import vgpx  # noqa: E402

EXPECTED = {
    'monitor': (355, '4579c62fb379f15d8e76881a1287a850bfad0b4aaff4e1384059cef08baea8fe'),
    'poster': (135, 'a5e06edaed44f929d0e09a0a6fad8beb37f702bec4e0ac49b0e4d5099e524c39'),
    'cd-box': (162, '53bd4921b6e42d1ac10feb93c5ccaac041a9bfbda3847b1b8bf6ee4819f83f35'),
}


class Samples(unittest.TestCase):
    def test_samples_decode_to_known_frames(self):
        for name, (count, digest) in EXPECTED.items():
            with self.subTest(sample=name):
                clip = vgpx.Clip(os.path.join(ROOT, 'samples', name + '.avi'))
                self.assertEqual((clip.width, clip.height, clip.fps), (88, 72, '1000000/33361'))
                dec = vgpx.Decoder(clip.width, clip.height, clip.extradata)
                h = hashlib.sha256()
                n = 0
                for frame in clip.frames(dec):
                    h.update(frame)
                    n += 1
                self.assertEqual(dec.bad_packets(), [])
                self.assertEqual(n, count)
                self.assertEqual(h.hexdigest(), digest)


if __name__ == '__main__':
    unittest.main()
