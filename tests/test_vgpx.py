"""Round-trip tests: a small VGPX encoder writes synthetic frames, and the decoder must return them exactly.

No public VGPX sample file exists, so these tests show that the decoder is consistent with the bitstream
rules in README.md. They cannot show that those rules match the original Alaris encoder; real clips did that.
Run with:  python3 -m unittest discover tests
"""
import os
import random
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vgpx  # noqa: E402

W, H, RUN = 88, 72, 4
D1_STEPS = sorted({vgpx.d1_value(v) for v in vgpx.D1.values()})
D2_STEPS = sorted({vgpx.d2_value(v) for v in vgpx.D2.values()})


def clamp(v):
    return min(max(v, 0), 255)


class BitWriter:
    def __init__(self):
        self.bits = []

    def code(self, codebook, value):
        s = next(k for k, v in codebook.items() if v == value)
        self.bits.extend(int(ch) for ch in reversed(s))   # last character is the first bit

    def tobytes(self):
        bits = self.bits + [0] * (-len(self.bits) % 16)    # packets are padded to 16 bits
        return bytes(sum(bits[i + k] << k for k in range(8)) for i in range(0, len(bits), 8))


def encode_diff(w, ref, px):
    """Op 9: find steps that land exactly on px from ref (the decoder clamps after adding)."""
    steps = []
    for k, table in ((0, D1_STEPS), (1, D2_STEPS), (2, D2_STEPS)):
        d = next(s for s in table if clamp(ref[k] + s) == px[k])
        steps.append(d)
    w.code(vgpx.INTRA1, 9) if w.intra else w.code(vgpx.INTER1, 9)
    w.code(vgpx.D1, steps[0] + 128)
    for d in steps[1:]:
        w.code(vgpx.D2, d + 128 if d <= 0 else d + 192)


def encode(target, prev=None):
    """Encode one frame. target and prev are lists of (c0, c1, c2) tuples in raster order."""
    n = W * H
    intra = prev is None
    book = vgpx.INTRA1 if intra else vgpx.INTER1
    buf = list(prev) if prev else [(0, 128, 128)] * n
    buf[0] = target[0]
    w = BitWriter()
    w.intra = intra
    i = 1
    while i < n:
        px, col = target[i], i % W
        left = buf[i - 1]
        if not intra and i + RUN <= n and all(buf[j] == target[j] for j in range(i, i + RUN)):
            w.code(book, 10); i += RUN; continue
        if not intra and buf[i] == px:
            w.code(book, 0); i += 1; continue
        if col and i + RUN <= n and all(target[j] == left for j in range(i, i + RUN)):
            w.code(book, 11)
            for j in range(i, i + RUN):
                buf[j] = left
            i += RUN; continue
        if col and px == left:
            w.code(book, 1)
        elif i >= W and px == buf[i - W]:
            w.code(book, 2)
        elif col and i >= W and px == tuple((a + b) >> 1 for a, b in zip(buf[i - W], left)):
            w.code(book, 5)
        elif not intra and i + 1 < n and px == buf[i + 1]:
            w.code(book, 3)
        elif not intra and i + W < n and px == buf[i + W]:
            w.code(book, 4)
        else:
            encode_diff(w, left if col else (0, 128, 128), px)
        buf[i] = px
        i += 1
    return bytes([1 if intra else 0, *target[0]]) + w.tobytes()


def random_picture(rng):
    """A picture built only from values the codec can reach, with flat runs and repeated rows."""
    pic = []
    for i in range(W * H):
        col = i % W
        r = rng.random()
        if col and r < 0.35:
            pic.append(pic[i - 1])
        elif i >= W and r < 0.55:
            pic.append(pic[i - W])
        else:
            ref = pic[i - 1] if col else (0, 128, 128)
            pic.append((clamp(ref[0] + rng.choice(D1_STEPS)),
                        clamp(ref[1] + rng.choice(D2_STEPS)),
                        clamp(ref[2] + rng.choice(D2_STEPS))))
    return pic


def planes(pic):
    return bytes(p[0] for p in pic) + bytes(p[1] for p in pic) + bytes(p[2] for p in pic)


def extradata():
    e = bytearray(32)
    e[4], e[5] = RUN, RUN
    return bytes(e)


def make_avi(path, packets, scale=33361, rate=1000000):
    def chunk(cid, body):
        return cid + struct.pack('<I', len(body)) + body + (b'\0' if len(body) & 1 else b'')

    def lst(kind, body):
        return chunk(b'LIST', kind + body)

    strh = b'vids' + b'VGPX' + struct.pack('<IHHIII', 0, 0, 0, 0, scale, rate) + bytes(32)
    bih = struct.pack('<IiiHH4sIiiII', 40, W, H, 1, 24, b'VGPX', 0, 0, 0, 0, 0)
    strl = lst(b'strl', chunk(b'strh', strh) + chunk(b'strf', bih + extradata()))
    hdrl = lst(b'hdrl', chunk(b'avih', bytes(56)) + strl)
    movi = lst(b'movi', b''.join(chunk(b'00dc', p) for p in packets))
    body = b'AVI ' + hdrl + movi
    with open(path, 'wb') as f:
        f.write(b'RIFF' + struct.pack('<I', len(body)) + body)


class RoundTrip(unittest.TestCase):
    def setUp(self):
        rng = random.Random(2000)
        self.pic1 = random_picture(rng)
        self.pic2 = list(self.pic1)
        for y in range(20, 50):                          # redraw a block for the inter frame
            for x in range(30, 70):
                ref = self.pic2[y * W + x - 1]
                self.pic2[y * W + x] = (clamp(ref[0] + rng.choice(D1_STEPS)),
                                        clamp(ref[1] + rng.choice(D2_STEPS)),
                                        clamp(ref[2] + rng.choice(D2_STEPS)))
        self.pkt1 = encode(self.pic1)
        self.pkt2 = encode(self.pic2, self.pic1)

    def test_codebooks_are_complete_and_prefix_free(self):
        for book in (vgpx.INTRA0, vgpx.INTER0, vgpx.INTRA1, vgpx.INTER1, vgpx.D1, vgpx.D2):
            vgpx.lut(book)

    def test_intra_then_inter_then_empty(self):
        dec = vgpx.Decoder(W, H, extradata())
        dec.decode(self.pkt1)
        self.assertEqual(dec.frame(), planes(self.pic1))
        dec.decode(self.pkt2)
        self.assertEqual(dec.frame(), planes(self.pic2))
        dec.decode(b'')
        self.assertEqual(dec.frame(), planes(self.pic2))
        self.assertEqual(dec.bad_packets(), [])
        self.assertEqual([s[0] for s in dec.stats], ['I', 'P', 'empty'])

    def test_unknown_flags_are_refused(self):
        dec = vgpx.Decoder(W, H, extradata())
        with self.assertRaises(vgpx.VGPXError):
            dec.decode(b'\x08' + self.pkt1[1:])

    def test_avi_to_y4m(self):
        with tempfile.TemporaryDirectory() as tmp:
            avi, y4m = os.path.join(tmp, 'clip.avi'), os.path.join(tmp, 'clip.y4m')
            make_avi(avi, [self.pkt1, self.pkt2, b''])
            clip = vgpx.Clip(avi)
            self.assertEqual((clip.width, clip.height, clip.fps), (W, H, '1000000/33361'))
            vgpx.write_y4m(clip, y4m)
            with open(y4m, 'rb') as f:
                data = f.read()
            header, rest = data.split(b'\n', 1)
            self.assertEqual(header, b'YUV4MPEG2 W88 H72 F1000000:33361 Ip A1:1 C444 XCOLORRANGE=FULL')
            frames = rest.split(b'FRAME\n')[1:]
            self.assertEqual(frames, [planes(self.pic1), planes(self.pic2), planes(self.pic2)])

    def test_command_line_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            avi = os.path.join(tmp, 'clip.avi')
            make_avi(avi, [self.pkt1, self.pkt2])
            script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'vgpx.py')
            r = subprocess.run([sys.executable, script, '--info', avi], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn('every packet decoded cleanly', r.stdout)


if __name__ == '__main__':
    unittest.main()
