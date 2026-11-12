"""Configuration loading; credentials are left to individual plugins."""
from __future__ import annotations
import os
import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from .safe_files import DEFAULT_MAX_PDF_BYTES

class ConfigError(RuntimeError):
    pass

DEFAULT_ATTACH_SETTLE_SECONDS = 20.0
MAX_ATTACH_SETTLE_SECONDS = 60.0

def validate_attach_settle_seconds(value: object) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConfigError('[fetch].attach_settle_seconds must be > 0 and <= 60') from exc
    if isinstance(value, bool) or not math.isfinite(seconds) or not 0 < seconds <= MAX_ATTACH_SETTLE_SECONDS:
        raise ConfigError('[fetch].attach_settle_seconds must be > 0 and <= 60')
    return seconds

@dataclass
class ZoteroConfig:
    local_api: str = 'http://127.0.0.1:23119/api/users/0'
    mcp_url: str = 'http://127.0.0.1:23120/mcp'
    collection_key: str = ''
    library_id: int = 1

@dataclass
class PathsConfig:
    archive_dir: Path
    staging_dir: Path
    state_db: Path
    report_dir: Path
    inbox_dir: Path

@dataclass
class FetchConfig:
    min_pdf_bytes: int = 20480
    title_similarity: float = 0.55
    http_timeout: int = 45
    source_order: list[str] = field(default_factory=lambda: ['openalex', 'unpaywall', 'arxiv'])
    enable_arxiv: bool = True
    enable_crossref: bool = False
    attach_settle_seconds: float = DEFAULT_ATTACH_SETTLE_SECONDS
    max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES
    use_env_proxy: bool = True

    def __post_init__(self):
        self.attach_settle_seconds = validate_attach_settle_seconds(self.attach_settle_seconds)
        if type(self.max_pdf_bytes) is not int or not 0 < self.max_pdf_bytes <= 1024**3:
            raise ConfigError('[fetch].max_pdf_bytes must be > 0 and <= 1073741824')
        if type(self.use_env_proxy) is not bool:
            raise ConfigError('[fetch].use_env_proxy must be a boolean')

@dataclass
class PluginConfig:
    enabled: list[str] = field(default_factory=list)
    directory: Path = Path('~/.config/zotero-pipeline/plugins')

@dataclass
class Config:
    zotero: ZoteroConfig
    paths: PathsConfig
    fetch: FetchConfig
    plugins: PluginConfig
    contact_email: str = ''
    report: dict = field(default_factory=dict)

def load_config(path: Path | None = None) -> Config:
    path = Path(path or 'config.toml').expanduser()
    try:
        with path.open('rb') as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f'无法读取配置：{type(exc).__name__}') from exc
    z, p, f, pl = (raw.get(k, {}) for k in ('zotero', 'paths', 'fetch', 'plugins'))
    def resolve(value: str) -> Path:
        result = Path(value).expanduser()
        return result if result.is_absolute() else Path(os.path.abspath(path.absolute().parent / result))
    paths = PathsConfig(resolve(p.get('archive_dir', '~/zotero-pdf-archive')),
        resolve(p.get('staging_dir', 'data/staging')), resolve(p.get('state_db', 'data/pipeline.db')),
        resolve(p.get('report_dir', 'data/reports')), resolve(p.get('inbox_dir', 'data/inbox')))
    for directory in (paths.archive_dir, paths.staging_dir, paths.report_dir, paths.inbox_dir, paths.state_db.parent):
        if directory.is_symlink(): raise ConfigError("symlink output directory refused")
        directory.mkdir(parents=True, exist_ok=True)
    plugin_dir = os.environ.get('ZPP_PLUGIN_DIR') or pl.get('directory', '~/.config/zotero-pipeline/plugins')
    return Config(ZoteroConfig(z.get('local_api', ZoteroConfig.local_api).rstrip('/'),
        z.get('mcp_url', ZoteroConfig.mcp_url), z.get('collection_key', ''), int(z.get('library_id', 1))),
        paths, FetchConfig(int(f.get('min_pdf_bytes', 20480)), float(f.get('title_similarity', .55)),
        int(f.get('http_timeout', 45)), list(f.get('source_order', ['openalex', 'unpaywall', 'arxiv'])),
        bool(f.get('enable_arxiv', True)), bool(f.get('enable_crossref', False)),
        f.get('attach_settle_seconds', DEFAULT_ATTACH_SETTLE_SECONDS), f.get('max_pdf_bytes', DEFAULT_MAX_PDF_BYTES),
        f.get('use_env_proxy', True)),
        PluginConfig(list(pl.get('enabled', [])), resolve(plugin_dir)),
        os.environ.get('ZPP_CONTACT_EMAIL') or raw.get('contact', {}).get('email', ''), raw.get('report', {}))
