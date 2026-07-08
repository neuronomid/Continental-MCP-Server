"""zstd-compressed JSONL raw archive (per market-hour).

Every external payload is archived verbatim so books/features can be replayed
and re-normalised if a parser changes (mcp_plan.md §5.3, risk #3).
"""

from __future__ import annotations

import datetime as dt
import os

import orjson
import zstandard as zstd


class RawArchive:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, stream: str, key: str, ts: dt.datetime) -> str:
        hour = ts.astimezone(dt.UTC).strftime("%Y%m%dT%H")
        d = os.path.join(self.root, stream, ts.strftime("%Y-%m-%d"))
        os.makedirs(d, exist_ok=True)
        safe_key = key.replace("/", "_")[:64]
        return os.path.join(d, f"{safe_key}_{hour}.jsonl.zst")

    def append(self, stream: str, key: str, record: dict, ts: dt.datetime | None = None) -> str:
        ts = ts or dt.datetime.now(dt.UTC)
        path = self._path(stream, key, ts)
        line = orjson.dumps(record) + b"\n"
        cctx = zstd.ZstdCompressor(level=3)
        with open(path, "ab") as fh:
            fh.write(cctx.compress(line))
        return path

    @staticmethod
    def read(path: str) -> list[dict]:
        data = _stream_decompress(path)
        return [orjson.loads(raw) for raw in data.splitlines() if raw.strip()]


def _stream_decompress(path: str) -> bytes:
    """Decompress a file that may contain multiple concatenated zstd frames.

    Each ``append`` writes an independent zstd frame, so reading must span
    frames.
    """
    dctx = zstd.ZstdDecompressor()
    chunks: list[bytes] = []
    with open(path, "rb") as fh:
        reader = dctx.stream_reader(fh, read_across_frames=True)
        while True:
            block = reader.read(65536)
            if not block:
                break
            chunks.append(block)
    return b"".join(chunks)
