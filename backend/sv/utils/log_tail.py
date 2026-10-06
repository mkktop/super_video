from pathlib import Path


def tail_lines(path: Path, count: int, max_bytes: int = 1024 * 1024) -> list[str]:
    """Bounded reverse read, including logs with a very long final line."""
    with path.open('rb') as stream:
        stream.seek(0, 2)
        position = stream.tell()
        chunks = []
        total = newlines = 0
        while position > 0 and newlines <= count and total < max_bytes:
            size = min(4096, position, max_bytes - total)
            position -= size
            stream.seek(position)
            block = stream.read(size)
            chunks.append(block)
            total += len(block)
            newlines += block.count(b'\n')
    return b''.join(reversed(chunks)).decode('utf-8', 'replace').splitlines()[-count:]
