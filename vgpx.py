#!/usr/bin/env python3
"""Decoder for Alaris VGPixel video (fourcc VGPX) stored in AVI files.

Usage:
    python3 vgpx.py INPUT.avi OUTPUT.mkv   lossless FFV1 video plus the original audio (needs ffmpeg)
    python3 vgpx.py INPUT.avi OUTPUT.mp4   H.264 scaled up 4x, audio as Apple Lossless (needs ffmpeg)
    python3 vgpx.py INPUT.avi OUTPUT.y4m   raw YUV 4:4:4 frames, no audio (Python only)
    python3 vgpx.py --info INPUT.avi       stream facts, and a check that every packet decodes cleanly

Only the Python standard library is used. README.md describes the bitstream.
"""
import os
import struct
import subprocess
import sys

__version__ = '1.0.0'

# Codebooks as printed on the MultimediaWiki page "Alaris VideoGramPiX".
# Bits are read least-significant first, so the LAST character of each string is the first bit read.
INTRA0 = {'0': 1, '011': 2, '111': 5, '01': 9}
INTER0 = {'11': 0, '00': 1, '110': 2, '11001': 3, '01001': 4, '0001': 5, '00101': 6, '10101': 7,
          '1101': 8, '010': 9}
INTRA1 = {'00': 1, '01': 2, '011': 5, '10': 9, '111': 11}
INTER1 = {'11': 0, '00': 1, '110': 2, '11001': 3, '01001': 4, '0001': 5, '000101': 6, '010101': 7,
          '1101': 8, '010': 9, '100101': 10, '110101': 11}
D1 = {'010100': 128, '000': 120, '001': 112, '1010': 104, '00100': 96, '00101': 88, '000010': 80,
      '110101': 72, '1010101': 64, '00010101': 56, '110010101': 48, '10001110100': 40, '01001110100': 32,
      '010101110100': 24, '110101110100': 16, '01101110100': 8, '00101110100': 248, '011001110100': 240,
      '111001110100': 232, '011101110100': 224, '111101110100': 216, '00001110100': 208, '010010101': 200,
      '11110100': 192, '0110100': 184, '100010': 176, '10010': 168, '1101': 160, '1100': 152, '110': 144,
      '11': 136}
D2 = {'0': 128, '001': 120, '1101': 112, '110101': 104, '11010101': 96, '01101010101': 88,
      '110101010101': 80, '011101010101': 72, '111101010101': 248, '010101010101': 240,
      '00101010101': 232, '001010101': 224, '0010101': 216, '00101': 208, '11': 200}


def d1_value(v):
    """First (brightness) component: a plain offset of 128, in steps of 8."""
    return v - 128


def d2_value(v):
    """Color components: values up to 128 are v - 128; values above 128 carry a bias of 192."""
    return v - 128 if v <= 128 else v - 192


def lut(codebook, value=lambda v: v):
    """12-bit lookup table: index = the next 12 bits (first bit in bit 0) -> (value, code length)."""
    table = [None] * 4096
    for s, v in codebook.items():
        code = 0
        for i, ch in enumerate(reversed(s)):
            code |= int(ch) << i
        n = len(s)
        for hi in range(1 << (12 - n)):
            assert table[code | (hi << n)] is None, 'codebook is not prefix-free'
            table[code | (hi << n)] = (value(v), n)
    assert all(e is not None for e in table), 'codebook is not complete'
    return table


T_D1 = lut(D1, d1_value)
T_D2 = lut(D2, d2_value)
CLAMP = [min(max(v, 0), 255) for v in range(-512, 768)]   # index: v + 512


class VGPXError(Exception):
    pass


class Decoder:
    """Holds the three component planes between frames and decodes one packet at a time."""

    def __init__(self, width, height, extradata=b''):
        self.W, self.H = width, height
        # Run lengths for ops 10 and 11 sit in bytes 4 and 5 of the 32-byte extradata.
        self.skipn = extradata[4] if len(extradata) > 5 else 4
        self.repn = extradata[5] if len(extradata) > 5 else 4
        if self.skipn or self.repn:
            self.t_intra, self.t_inter = lut(INTRA1), lut(INTER1)
        else:
            self.t_intra, self.t_inter = lut(INTRA0), lut(INTER0)
        n = width * height
        self.c = [[0] * n, [128] * n, [128] * n]
        self.stats = []   # per packet: (kind, pixels decoded, bits left over)

    def decode(self, pkt):
        W = self.W
        n = W * self.H
        if not pkt:                                       # empty packet: repeat the previous frame
            self.stats.append(('empty', n, 0))
            return
        if len(pkt) < 4 or pkt[0] not in (0, 1):
            raise VGPXError('unsupported frame header %r: only flags 0x00 (inter) and 0x01 (intra) '
                            'have been seen in real files' % bytes(pkt[:4]))
        intra = pkt[0] == 1
        ops = self.t_intra if intra else self.t_inter
        data = bytes(pkt[4:]) + b'\0\0\0\0'
        nbits = (len(pkt) - 4) * 8
        c0, c1, c2 = self.c
        c0[0], c1[0], c2[0] = pkt[1], pkt[2], pkt[3]    # bytes 1-3 are pixel 0
        TD1, TD2, CL = T_D1, T_D2, CLAMP
        skipn, repn = self.skipn, self.repn
        fb = int.from_bytes
        p = 0
        i = 1
        while i < n and p < nbits:
            op, l = ops[(fb(data[p >> 3:(p >> 3) + 3], 'little') >> (p & 7)) & 0xFFF]
            p += l
            if op == 9:                                   # difference
                a, l = TD1[(fb(data[p >> 3:(p >> 3) + 3], 'little') >> (p & 7)) & 0xFFF]; p += l
                b, l = TD2[(fb(data[p >> 3:(p >> 3) + 3], 'little') >> (p & 7)) & 0xFFF]; p += l
                cc, l = TD2[(fb(data[p >> 3:(p >> 3) + 3], 'little') >> (p & 7)) & 0xFFF]; p += l
                if i % W:
                    r0, r1, r2 = c0[i - 1], c1[i - 1], c2[i - 1]
                else:                                     # column 0: the reference is black
                    r0, r1, r2 = 0, 128, 128
                c0[i] = CL[r0 + a + 512]; c1[i] = CL[r1 + b + 512]; c2[i] = CL[r2 + cc + 512]
                i += 1
            elif op == 1:                                 # copy left
                c0[i] = c0[i - 1]; c1[i] = c1[i - 1]; c2[i] = c2[i - 1]; i += 1
            elif op == 0:                                 # skip: keep the previous frame's pixel
                i += 1
            elif op == 2:                                 # copy top
                j = i - W if i >= W else i - 1
                c0[i] = c0[j]; c1[i] = c1[j]; c2[i] = c2[j]; i += 1
            elif op == 5:                                 # average of top and left
                j = i - W if i >= W else i - 1
                c0[i] = (c0[j] + c0[i - 1]) >> 1; c1[i] = (c1[j] + c1[i - 1]) >> 1; c2[i] = (c2[j] + c2[i - 1]) >> 1
                i += 1
            elif op == 11:                                # repeat left pixel N times
                e = min(i + repn, n)
                v0, v1, v2 = c0[i - 1], c1[i - 1], c2[i - 1]
                while i < e:
                    c0[i] = v0; c1[i] = v1; c2[i] = v2; i += 1
            elif op == 10:                                # skip N pixels
                i += skipn
            elif op == 3:                                 # copy right (still the previous frame)
                j = i + 1 if i + 1 < n else i
                c0[i] = c0[j]; c1[i] = c1[j]; c2[i] = c2[j]; i += 1
            elif op == 4:                                 # copy bottom (still the previous frame)
                j = i + W if i + W < n else i
                c0[i] = c0[j]; c1[i] = c1[j]; c2[i] = c2[j]; i += 1
            elif op == 6:                                 # average of top and right
                t = i - W if i >= W else i
                r = i + 1 if i + 1 < n else i
                c0[i] = (c0[t] + c0[r]) >> 1; c1[i] = (c1[t] + c1[r]) >> 1; c2[i] = (c2[t] + c2[r]) >> 1; i += 1
            elif op == 7:                                 # average of bottom and right
                b_ = i + W if i + W < n else i
                r = i + 1 if i + 1 < n else i
                c0[i] = (c0[b_] + c0[r]) >> 1; c1[i] = (c1[b_] + c1[r]) >> 1; c2[i] = (c2[b_] + c2[r]) >> 1; i += 1
            elif op == 8:                                 # average of bottom and left
                b_ = i + W if i + W < n else i
                c0[i] = (c0[b_] + c0[i - 1]) >> 1; c1[i] = (c1[b_] + c1[i - 1]) >> 1; c2[i] = (c2[b_] + c2[i - 1]) >> 1
                i += 1
        self.stats.append(('I' if intra else 'P', i, nbits - p))

    def frame(self):
        """The current picture as planar YUV 4:4:4 bytes (Y, Cb, Cr), full range."""
        return bytes(self.c[0]) + bytes(self.c[1]) + bytes(self.c[2])

    def bad_packets(self):
        n = self.W * self.H
        return [k for k, (kind, px, left) in enumerate(self.stats)
                if kind != 'empty' and (px != n or not 0 <= left < 16)]


class Clip:
    """The parts of an AVI file the decoder needs."""

    def __init__(self, path):
        with open(path, 'rb') as f:
            d = f.read()
        if d[:4] != b'RIFF' or d[8:12] != b'AVI ':
            raise VGPXError('%s is not an AVI file' % path)
        streams = []                  # [fccType, strh body, strf body]
        chunks = {}                   # stream number -> list of chunk bodies

        def walk(off, end):
            while off + 8 <= end:
                cid = d[off:off + 4]
                sz = struct.unpack('<I', d[off + 4:off + 8])[0]
                if cid in (b'RIFF', b'LIST'):
                    walk(off + 12, min(off + 8 + sz, end))
                else:
                    body = d[off + 8:off + 8 + sz]
                    if cid == b'strh':
                        streams.append([body[:4], body, b''])
                    elif cid == b'strf' and streams:
                        streams[-1][2] = body
                    elif cid[:2].isdigit() and cid[2:] in (b'dc', b'db', b'wb'):
                        chunks.setdefault(int(cid[:2]), []).append(body)
                off += 8 + sz + (sz & 1)

        walk(12, len(d))
        video = [k for k, s in enumerate(streams) if s[0] == b'vids']
        if not video:
            raise VGPXError('%s has no video stream' % path)
        k = video[0]
        _, strh, strf = streams[k]
        if strf[16:20].upper() != b'VGPX':
            raise VGPXError('%s is %r video, not VGPX' % (path, strf[16:20].decode('latin-1')))
        self.width, h = struct.unpack('<ii', strf[4:12])
        self.height = abs(h)
        self.extradata = strf[40:]
        self.scale, self.rate = struct.unpack('<II', strh[20:28])
        self.packets = chunks.get(k, [])
        self.has_audio = any(s[0] == b'auds' for s in streams)

    @property
    def fps(self):
        return '%d/%d' % (self.rate, self.scale)

    def frames(self, decoder=None):
        dec = decoder or Decoder(self.width, self.height, self.extradata)
        for pkt in self.packets:
            dec.decode(pkt)
            yield dec.frame()


def write_y4m(clip, out):
    with open(out, 'wb') as f:
        f.write(b'YUV4MPEG2 W%d H%d F%d:%d Ip A1:1 C444 XCOLORRANGE=FULL\n'
                % (clip.width, clip.height, clip.rate, clip.scale))
        for frame in clip.frames():
            f.write(b'FRAME\n')
            f.write(frame)


def write_ffmpeg(clip, src, out):
    W, H = clip.width, clip.height
    raw = ['-f', 'rawvideo', '-pix_fmt', 'yuv444p', '-s', '%dx%d' % (W, H), '-framerate', clip.fps,
           '-color_range', 'pc', '-colorspace', 'bt470bg', '-i', '-']
    audio_in = ['-i', src] if clip.has_audio else []
    maps = ['-map', '0:v'] + (['-map', '1:a'] if clip.has_audio else [])
    if out.lower().endswith('.mkv'):
        codec = ['-c:v', 'ffv1', '-level', '3', '-g', '1', '-color_range', 'pc', '-colorspace', 'bt470bg',
                 '-color_primaries', 'bt470bg', '-color_trc', 'gamma28', '-c:a', 'copy']
    else:
        codec = ['-vf', 'scale=%d:%d:flags=lanczos:in_range=pc:in_color_matrix=bt601:'
                        'out_color_matrix=bt709:out_range=tv,format=yuv420p' % (W * 4, H * 4),
                 '-c:v', 'libx264', '-crf', '12', '-preset', 'slow',
                 '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-color_range', 'tv',
                 '-c:a', 'alac', '-movflags', '+faststart']
    cmd = ['ffmpeg', '-v', 'error', '-y'] + raw + audio_in + maps + codec + [out]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        for frame in clip.frames():
            p.stdin.write(frame)
    finally:
        p.stdin.close()
    if p.wait() != 0:
        raise VGPXError('ffmpeg failed')


def info(clip):
    dec = Decoder(clip.width, clip.height, clip.extradata)
    for _ in clip.frames(dec):
        pass
    kinds = [s[0] for s in dec.stats]
    bad = dec.bad_packets()
    print('%dx%d at %s fps, %d packets: %d intra, %d inter, %d empty; audio: %s'
          % (clip.width, clip.height, clip.fps, len(kinds), kinds.count('I'), kinds.count('P'),
             kinds.count('empty'), 'yes' if clip.has_audio else 'no'))
    print('every packet decoded cleanly' if not bad else 'packets that did not decode cleanly: %s' % bad)
    return 0 if not bad else 1


def main(argv):
    if len(argv) == 2 and argv[0] == '--info':
        return info(Clip(argv[1]))
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    src, out = argv
    clip = Clip(src)
    ext = os.path.splitext(out)[1].lower()
    if ext == '.y4m':
        write_y4m(clip, out)
    elif ext in ('.mkv', '.mp4'):
        write_ffmpeg(clip, src, out)
    else:
        print('the output name must end in .mkv, .mp4 or .y4m', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except VGPXError as e:
        print('vgpx: %s' % e, file=sys.stderr)
        sys.exit(1)
