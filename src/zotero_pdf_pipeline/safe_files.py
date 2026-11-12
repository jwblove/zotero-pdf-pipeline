"""Confined, exclusive file publication; no symlink targets or replacement writes."""
from __future__ import annotations
import os
import re
import stat
import uuid
from pathlib import Path

DEFAULT_MAX_PDF_BYTES = 100 * 1024 * 1024

class UnsafePath(ValueError):
    pass

def safe_key(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
        raise UnsafePath('invalid item key')
    return value

def regular_file(path: Path, max_bytes: int = DEFAULT_MAX_PDF_BYTES) -> bool:
    try:
        info = path.lstat()
        return stat.S_ISREG(info.st_mode) and info.st_size <= max_bytes
    except OSError:
        return False

def target(root: Path, name: str) -> Path:
    if not name or name in ('.', '..') or Path(name).name != name or '/' in name or '\\' in name:
        raise UnsafePath('invalid target name')
    if root.is_symlink():
        raise UnsafePath('symlink root refused')
    root.mkdir(parents=True, exist_ok=True)
    result = root.resolve() / name
    if result.is_symlink() or not result.resolve().is_relative_to(root.resolve()):
        raise UnsafePath('symlink or escaping target refused')
    return result

def publish(root: Path, name: str, chunks, *, max_bytes=DEFAULT_MAX_PDF_BYTES, unique=True) -> Path:
    dest = target(root, name)
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    directory = os.open(str(root.resolve()), flags)
    temporary = '.partial-' + uuid.uuid4().hex
    created = False
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600, dir_fd=directory)
        created = True
        with os.fdopen(fd, 'wb') as output:
            total = 0
            for chunk in chunks:
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError('file size limit exceeded')
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        for index in range(10000):
            addition = f' {index}{Path(name).suffix}'
            candidate = name if index == 0 else Path(name).stem.encode()[:230-len(addition.encode())].decode('utf-8','ignore') + addition
            dest = target(root, candidate)
            try:
                # Linking publishes a fully written file without replacing an existing name.
                os.link(temporary, candidate, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                return dest
            except FileExistsError:
                if dest.is_symlink() or not unique:
                    raise UnsafePath('existing target refused')
        raise UnsafePath('no unused filename')
    finally:
        try:
            if created: os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)

def write_bytes(root: Path, name: str, content: bytes, **kwargs) -> Path:
    return publish(root, name, [content], **kwargs)

def copy_file(source: Path, root: Path, name: str, *, max_bytes=DEFAULT_MAX_PDF_BYTES) -> Path:
    if not regular_file(source, max_bytes):
        raise UnsafePath('source must be a bounded regular file')
    fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise UnsafePath('source is not regular')
        return publish(root, name, iter(lambda: stream.read(65536), b''), max_bytes=max_bytes)
