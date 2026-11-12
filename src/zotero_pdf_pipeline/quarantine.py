"""Attempt confined quarantine; never move an existing archive or follow symlinks."""
from pathlib import Path
from .safe_files import copy_file, safe_key, DEFAULT_MAX_PDF_BYTES

def quarantine_pdf(path: Path, staging: Path, item_key: str, *, max_bytes=DEFAULT_MAX_PDF_BYTES) -> Path:
    safe_key(item_key)
    if path.is_symlink(): raise ValueError('symlink source refused')
    source = path.resolve(strict=True)
    staging = staging.resolve()
    rejected = staging.parent / 'staging-rejected'
    name = (item_key + '-' + source.stem).encode()[:210].decode('utf-8','ignore') + '.pdf'
    destination = copy_file(source, rejected, name, max_bytes=max_bytes)
    # Publish the complete quarantine copy before removing a staging source.
    if source.is_relative_to(staging):
        source.unlink()
    return destination
