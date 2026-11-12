"""Opt-in source plugin contract.

A source has a stable name and fetch(item_meta, dest_dir) method. item_meta is
a PaperNeed with item_key, title, doi, authors (list of names), year, item_type,
publication, collection_key, pmcid and has_institutional_authors (bool, default
False); string/list fields other than item_key may be empty.
Return a candidate Path or None when unavailable. Exceptions denote failures;
the core records their type, not their text. fetch validates/archives candidates and attempts quarantine on rejection; it does
not attach. The separate attach phase revalidates and requires MCP/local read-back.

Plugins self-throttle, honour Retry-After, must not automate CAPTCHA or login
bypasses, write only within dest_dir, and obtain credentials only from environment
variables. Enforce public HTTPS/DNS and guarded redirects, bounded streaming and
exclusive writes (the public_http/safe_files helpers implement the reference
policy). Core copy/validation guards cannot constrain arbitrary plugin code.
Names match [A-Za-z][A-Za-z0-9_]*; exports may be instances or zero-argument classes.
Python plugins run with user permissions; this is not a sandbox.
"""
from __future__ import annotations
from importlib import metadata, util
from pathlib import Path
from typing import Protocol
import re
import sys
from .models import PaperNeed, ResolvedFile
from .safe_files import copy_file, safe_key, DEFAULT_MAX_PDF_BYTES

_FILE_MODULES: dict[Path, object] = {}

class Source(Protocol):
    """A plugin exports ``source`` (or entry point object) with this interface.

    fetch returns a candidate path, or None when unavailable. Plugin exceptions are
    deliberately redacted. Plugins manage their own environment credentials.
    """
    name: str
    def fetch(self, item_meta: PaperNeed, dest_dir: Path) -> Path | None: ...

def load_source(name: str, directory: Path) -> Source:
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', name):
        raise ValueError('invalid plugin name')
    matches = list(metadata.entry_points(group='zotero_pdf_pipeline.sources', name=name))
    path = directory / f'{name}.py'
    if len(matches) + int(path.is_file()) != 1:
        raise ValueError('plugin missing or ambiguous')
    if matches:
        source = matches[0].load()
    else:
        cache_key = path.resolve()
        module = _FILE_MODULES.get(cache_key)
        if module is None:
            spec = util.spec_from_file_location(f'zpp_plugin_{name}', path)
            if spec is None or spec.loader is None:
                raise ValueError('plugin cannot be loaded')
            module = util.module_from_spec(spec)
            sys.modules[spec.name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(spec.name, None)
                raise
            _FILE_MODULES[cache_key] = module
        source = module.source
    if isinstance(source, type):
        source = source()
    if source.name != name or not callable(source.fetch):
        raise ValueError('invalid plugin interface')
    return source

def fetch_plugin(name: str, directory: Path, paper: PaperNeed, staging: Path,
                 *, max_pdf_bytes=DEFAULT_MAX_PDF_BYTES) -> ResolvedFile | None:
    safe_key(paper.item_key)
    source = load_source(name, directory)
    candidate = source.fetch(paper, staging)
    if candidate is None:
        return None
    candidate = Path(candidate)
    # Copy into staging so quarantine never moves a plugin's original file.
    dest = copy_file(candidate, staging, f'plugin-{paper.item_key}-{name}.pdf', max_bytes=max_pdf_bytes)
    return ResolvedFile(paper, dest, name)
