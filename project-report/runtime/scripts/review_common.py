"""Shared file hashing, project credential lookup and literal quote recovery."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unicodedata
from pathlib import Path

MODEL_KEY_NAMES = {'DEEPSEEK_API_KEY', 'DASHSCOPE_API_KEY', 'SILICONFLOW_API_KEY'}


def atomic_write_bytes(path: Path, value: bytes):
    """Keep the old file intact while Windows readers/scanners briefly hold it."""
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent,
                prefix='.atomic-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name); stream.write(value)
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                return
            except OSError as error:
                if getattr(error, 'winerror', None) not in {5, 32, 33} or attempt == 5:
                    raise
                time.sleep(min(.03 * 2 ** attempt, .4))
    finally:
        if temporary is not None:
            try: temporary.unlink(missing_ok=True)
            except OSError: pass


def atomic_write_json(path: Path, value):
    atomic_write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def api_key(name: str = 'SILICONFLOW_API_KEY') -> str | None:
    """Prefer a process secret; otherwise read the project's private 0600 .env."""
    if name not in MODEL_KEY_NAMES:
        raise ValueError('Unsupported model credential name')
    value = os.environ.get(name)
    if value:
        return value.strip()
    path = Path(__file__).resolve().parent.parent / "config" / ".env"
    if not path.exists():
        return None
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Key file must not be accessible to group or others")
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(name + '='):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def recover_source_quote(model_quote: object, source: str) -> str | None:
    """Locate text despite whitespace/quote typography and return literal source bytes."""
    if not isinstance(model_quote, str) or not model_quote:
        return None
    ignored = set('“”"‘’')
    compact_chars: list[str] = []
    positions: list[int] = []
    for position, char in enumerate(source):
        if char.isspace() or char in ignored:
            continue
        for normalized_char in unicodedata.normalize("NFKC", char):
            compact_chars.append(normalized_char)
            positions.append(position)
    needle = "".join(
        unicodedata.normalize("NFKC", char)
        for char in model_quote
        if not char.isspace() and char not in ignored
    )
    if not needle:
        return None
    offset = "".join(compact_chars).find(needle)
    if offset < 0:
        return None
    return source[positions[offset] : positions[offset + len(needle) - 1] + 1]
