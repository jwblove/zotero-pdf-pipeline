"""Confined, exclusive file publication; no symlink targets or replacement writes."""
from __future__ import annotations
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import PureWindowsPath
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
    if os.name == 'nt':
        reserved = os.path.isreserved(name) if hasattr(os.path, 'isreserved') else PureWindowsPath(name).is_reserved()
        if reserved or any(c in '<>:"|?*' or ord(c) < 32 for c in name) or name.endswith((' ', '.')):
            raise UnsafePath('invalid Windows filename')
    if root.is_symlink():
        raise UnsafePath('symlink root refused')
    root.mkdir(parents=True, exist_ok=True)
    result = root.resolve() / name
    if result.is_symlink() or not result.resolve().is_relative_to(root.resolve()):
        raise UnsafePath('symlink or escaping target refused')
    return result

def publish(root: Path, name: str, chunks, *, max_bytes=DEFAULT_MAX_PDF_BYTES, unique=True) -> Path:
    target(root, name)
    root = root.absolute()
    with _publication_directory(root) as directory:
        return _publish(root, directory, name, chunks, max_bytes, unique)


@contextmanager
def _publication_directory(root):
    if os.name == 'nt':
        from .windows_files import pinned_directory
        with pinned_directory(root):
            yield None
    else:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory = os.open(root, flags)
        try:
            yield directory
        finally:
            os.close(directory)


def _publish(root, directory, name, chunks, max_bytes, unique):
    temporary = '.partial-' + uuid.uuid4().hex
    temp_path = root / temporary if directory is None else temporary
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
    fd = os.open(temp_path, flags, 0o600, dir_fd=directory)
    cleanup = True
    try:
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
                if directory is None:
                    # Windows rename fails if the destination already exists.
                    os.rename(temp_path, dest)
                    cleanup = False
                    return dest
                os.link(temporary, candidate, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                return dest
            except FileExistsError:
                if dest.is_symlink() or not unique:
                    raise UnsafePath('existing target refused')
        raise UnsafePath('no unused filename')
    finally:
        try:
            if cleanup:
                os.unlink(temp_path, dir_fd=directory)
        except FileNotFoundError:
            pass

def write_bytes(root: Path, name: str, content: bytes, **kwargs) -> Path:
    return publish(root, name, [content], **kwargs)

def copy_file(source: Path, root: Path, name: str, *, max_bytes=DEFAULT_MAX_PDF_BYTES) -> Path:
    if not regular_file(source, max_bytes):
        raise UnsafePath('source must be a bounded regular file')
    if os.name == 'nt':
        from .windows_files import read_fd
        fd = read_fd(source)
    else:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise UnsafePath('source is not regular')
        return publish(root, name, iter(lambda: stream.read(65536), b''), max_bytes=max_bytes)
