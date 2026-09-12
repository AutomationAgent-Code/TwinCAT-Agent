"""Materialize private bundled fallback scripts for this process only.

Release resources live in interpreter-specific bytecode, not loose source.
This is packaging/obfuscation, not a confidentiality or encryption boundary.
"""
import base64
import hashlib
from pathlib import Path
import tempfile
import threading
import zlib

_lock = threading.Lock()
_directory = None


def script_path(name: str) -> Path:
    global _directory
    from ._script_bundle import SCRIPTS
    if name not in SCRIPTS:
        raise FileNotFoundError(f'Packaged script not found: {name}')
    with _lock:
        if _directory is None:
            directory = tempfile.TemporaryDirectory(prefix='TwinCATAgent-scripts-')
            try:
                for filename, (digest, encoded) in SCRIPTS.items():
                    if Path(filename).name != filename or not filename.endswith('.ps1'):
                        raise ValueError('Invalid packaged script name')
                    data = zlib.decompress(base64.b85decode(encoded))
                    if hashlib.sha256(data).hexdigest() != digest:
                        raise ValueError('Packaged script integrity mismatch')
                    Path(directory.name, filename).write_bytes(data)
            except Exception:
                directory.cleanup()
                raise
            _directory = directory
        return Path(_directory.name, name)
