"""Minimal typed NBT reader/writer to remove template-world player state."""
import gzip
import io
import struct


def sanitize_level(data):
    compressed = data.startswith(b"\x1f\x8b")
    stream = io.BytesIO(gzip.decompress(data) if compressed else data)

    def number(fmt):
        size = struct.calcsize(fmt)
        raw = stream.read(size)
        if len(raw) != size:
            raise ValueError("Truncated world NBT")
        return struct.unpack(fmt, raw)[0]

    def string():
        length = number(">H")
        raw = stream.read(length)
        if len(raw) != length:
            raise ValueError("Truncated NBT string")
        # Preserve Java modified-UTF8 names byte-for-byte.
        return raw

    formats = {1: ">b", 2: ">h", 3: ">i", 4: ">q", 5: ">f", 6: ">d"}

    def payload(kind, depth=0):
        if depth > 100:
            raise ValueError("NBT nesting limit exceeded")
        if kind in formats:
            return number(formats[kind])
        if kind in (7, 11, 12):
            length = number(">i")
            width = {7: 1, 11: 4, 12: 8}[kind]
            if length < 0 or length * width > len(data) * 1000:
                raise ValueError("Invalid NBT array length")
            raw = stream.read(length * width)
            if len(raw) != length * width:
                raise ValueError("Truncated NBT array")
            return length, raw
        if kind == 8:
            return string()
        if kind == 9:
            element = number(">B")
            count = number(">i")
            if count < 0 or count > 10000000:
                raise ValueError("Invalid NBT list length")
            return element, [payload(element, depth + 1) for _ in range(count)]
        if kind == 10:
            compound = {}
            while True:
                child = number(">B")
                if child == 0:
                    return compound
                name = string()
                if name in compound:
                    raise ValueError("Duplicate NBT tag")
                compound[name] = child, payload(child, depth + 1)
        raise ValueError(f"Unsupported NBT tag {kind}")

    root_type = number(">B")
    name = string()
    if root_type != 10:
        raise ValueError("World NBT root must be compound")
    root = payload(root_type)
    if stream.read(1):
        raise ValueError("Trailing NBT data")
    if root.get(b"Data", (0,))[0] != 10:
        raise ValueError("World level.dat missing Data compound")
    world = root[b"Data"][1]
    world.pop(b"Player", None)
    world[b"GameType"] = (3, 0)
    # Do not change terrain, spawn coordinates, rules, commands or datapack logic.
    output = io.BytesIO()

    def emit_string(value):
        output.write(struct.pack(">H", len(value)))
        output.write(value)

    def emit(kind, value):
        if kind in formats:
            output.write(struct.pack(formats[kind], value))
        elif kind in (7, 11, 12):
            output.write(struct.pack(">i", value[0]))
            output.write(value[1])
        elif kind == 8:
            emit_string(value)
        elif kind == 9:
            output.write(struct.pack(">Bi", value[0], len(value[1])))
            for child in value[1]:
                emit(value[0], child)
        elif kind == 10:
            for key, (child_type, child) in value.items():
                output.write(bytes([child_type]))
                emit_string(key)
                emit(child_type, child)
            output.write(b"\x00")

    output.write(bytes([root_type]))
    emit_string(name)
    emit(root_type, root)
    raw = output.getvalue()
    if compressed:
        packed = gzip.compress(raw, mtime=0)
        # Python 3.11/zlib varies the OS header byte across Windows/Linux.
        return packed[:9] + b"\xff" + packed[10:]
    return raw
