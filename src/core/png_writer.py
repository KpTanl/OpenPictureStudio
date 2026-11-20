"""8 位 PNG 分行带并行滤波和压缩；每行的滤波选法与 Pillow 相同，解出的像素与 Pillow 写的一致。"""

import os
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor

BAND_ROWS = 64
_ADLER_BASE = 65521


def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(data, zlib.crc32(kind)))


def _adler_combine(first, second, length):
    remainder = length % _ADLER_BASE
    low = (first & 0xFFFF) + (second & 0xFFFF) + _ADLER_BASE - 1
    high = remainder * (first & 0xFFFF) % _ADLER_BASE + (first >> 16) + (second >> 16) + _ADLER_BASE - remainder
    return low % _ADLER_BASE | high % _ADLER_BASE << 16


def write_png(pixels, target, *, channels, threads=1, level=6):
    """pixels 为 HxWxC 的连续 uint8，写出前 channels 个通道（3 为 RGB，4 为 RGBA）；target 为路径或二进制流。"""
    import ops_accel

    height, width = pixels.shape[:2]
    bands = range(0, height, BAND_ROWS)

    def encode(top):
        data = ops_accel.png_filter_rows(pixels, top, min(height, top + BAND_ROWS), channels)
        compressor = zlib.compressobj(level, zlib.DEFLATED, -15)
        last = top + BAND_ROWS >= height
        packed = compressor.compress(data) + compressor.flush(zlib.Z_FINISH if last else zlib.Z_SYNC_FLUSH)
        return packed, zlib.adler32(data), data.size

    stream = open(target, "wb") if isinstance(target, (str, os.PathLike)) else target
    try:
        stream.write(b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8,
                                                                         6 if channels == 4 else 2, 0, 0, 0)))
        adler = 1
        with ThreadPoolExecutor(max_workers=max(1, threads), thread_name_prefix="ops-png") as pool:
            for index, (packed, checksum, length) in enumerate(pool.map(encode, bands)):
                adler = _adler_combine(adler, checksum, length)
                if index == 0:
                    packed = b"\x78\x9c" + packed
                if index == len(bands) - 1:
                    packed += struct.pack(">I", adler)
                stream.write(_chunk(b"IDAT", packed))
        stream.write(_chunk(b"IEND", b""))
    finally:
        if stream is not target:
            stream.close()
