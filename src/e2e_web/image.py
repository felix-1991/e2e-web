"""Small PNG downscaler for model-bound screenshots.

The SDK has no image-codec dependency, and the screenshots it captures are
8-bit PNGs without interlacing. A long side of 768 keeps a full step of margin
above the size at which models start misreading drawn controls while cutting
the image tokens a large viewport costs every turn. Anything this decoder does
not understand is returned unchanged, so an unusual capture never fails a step.
"""

import struct
import zlib

MAX_SCREENSHOT_LONG_SIDE = 768
_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}


def _chunks(data: bytes):
    if not data.startswith(_SIGNATURE):
        raise ValueError("not a PNG")
    offset = 8
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + length]
        if len(body) != length:
            raise ValueError("truncated chunk")
        yield kind, body
        offset += 12 + length
        if kind == b"IEND":
            return


def _header(data: bytes):
    header = None
    compressed = []
    for kind, body in _chunks(data):
        if kind == b"IHDR":
            if len(body) != 13:
                raise ValueError("bad IHDR")
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            compressed.append(body)
    if header is None or not compressed:
        raise ValueError("no image data")
    return header, b"".join(compressed)


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> bytearray:
    stride = width * bpp
    if len(raw) < (stride + 1) * height:
        raise ValueError("short image data")
    pixels = bytearray(width * height * bpp)
    previous = bytearray(stride)
    offset = 0
    for row_index in range(height):
        filter_type = raw[offset]
        offset += 1
        row = bytearray(raw[offset:offset + stride])
        offset += stride
        if filter_type == 1:
            for index in range(bpp, stride):
                row[index] = (row[index] + row[index - bpp]) & 0xFF
        elif filter_type == 2:
            for index in range(stride):
                row[index] = (row[index] + previous[index]) & 0xFF
        elif filter_type == 3:
            for index in range(stride):
                left = row[index - bpp] if index >= bpp else 0
                row[index] = (row[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif filter_type == 4:
            for index in range(stride):
                left = row[index - bpp] if index >= bpp else 0
                above = previous[index]
                above_left = previous[index - bpp] if index >= bpp else 0
                estimate = left + above - above_left
                da = abs(estimate - left)
                db = abs(estimate - above)
                dc = abs(estimate - above_left)
                predictor = left if da <= db and da <= dc else (above if db <= dc else above_left)
                row[index] = (row[index] + predictor) & 0xFF
        elif filter_type != 0:
            raise ValueError("unsupported PNG filter")
        pixels[row_index * stride:(row_index + 1) * stride] = row
        previous = row
    return pixels


def _resize(pixels: bytearray, width: int, height: int, bpp: int,
            target_width: int, target_height: int) -> bytearray:
    spans = []
    for x in range(target_width):
        start = x * width // target_width
        end = max(start + 1, (x + 1) * width // target_width)
        spans.append((start, end))
    output = bytearray(target_width * target_height * bpp)
    offset = 0
    for y in range(target_height):
        first = y * height // target_height
        last = max(first + 1, (y + 1) * height // target_height)
        count = (last - first)
        for start, end in spans:
            divisor = count * (end - start)
            for channel in range(bpp):
                total = 0
                for source_y in range(first, last):
                    base = source_y * width * bpp
                    for source_x in range(start, end):
                        total += pixels[base + source_x * bpp + channel]
                output[offset] = total // divisor
                offset += 1
    return output


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(
        ">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def _encode(pixels: bytearray, width: int, height: int, bpp: int) -> bytes:
    color_type = {1: 0, 2: 4, 3: 2, 4: 6}[bpp]
    stride = width * bpp
    rows = b"".join(b"\x00" + bytes(pixels[y * stride:(y + 1) * stride]) for y in range(height))
    return (_SIGNATURE
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(rows, 6))
            + _chunk(b"IEND", b""))


def downscale_png(data: bytes, max_long_side: int = MAX_SCREENSHOT_LONG_SIDE) -> bytes:
    """Return a smaller PNG, or the input untouched when it cannot be decoded."""
    if not isinstance(data, bytes) or max_long_side <= 0:
        return data
    try:
        header, compressed = _header(data)
        width, height, depth, color_type, _compression, _filter, interlace = header
        if depth != 8 or interlace != 0 or color_type not in _CHANNELS:
            return data
        if max(width, height) <= max_long_side:
            return data
        bpp = _CHANNELS[color_type]
        pixels = _unfilter(zlib.decompress(compressed), width, height, bpp)
        factor = max_long_side / max(width, height)
        target_width = max(1, round(width * factor))
        target_height = max(1, round(height * factor))
        return _encode(_resize(pixels, width, height, bpp, target_width, target_height),
                       target_width, target_height, bpp)
    except (ValueError, zlib.error, struct.error, MemoryError):
        return data
