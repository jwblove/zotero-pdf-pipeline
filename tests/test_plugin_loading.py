"""File plugin loading and redacted diagnostics, without network access."""
import logging
import sys
from pathlib import Path

import pytest

from zotero_pdf_pipeline import cli, plugins
from zotero_pdf_pipeline.config import load_config
from zotero_pdf_pipeline.models import PaperNeed
from zotero_pdf_pipeline.state import Store


def test_file_plugin_dataclass_loads_and_fetches(tmp_path):
    path = tmp_path / 'data_source.py'
    path.write_text('''from dataclasses import dataclass
@dataclass
class DataSource:
    name: str = "data_source"
    def fetch(self, item_meta, dest_dir):
        result = dest_dir / "candidate.pdf"
        result.write_bytes(b"%PDF-test")
        return result
source = DataSource()
''')
    module_name = 'zpp_plugin_data_source'
    try:
        plugin = plugins.load_source('data_source', tmp_path)
        assert sys.modules[module_name].source is plugin
        candidate = plugin.fetch(PaperNeed('K', 'Title'), tmp_path)
        assert candidate.read_bytes() == b'%PDF-test'
        assert plugins.load_source('data_source', tmp_path) is plugin
    finally:
        plugins._FILE_MODULES.pop(path.resolve(), None)
        sys.modules.pop(module_name, None)


def test_import_failure_removes_module_then_fixed_file_loads(tmp_path):
    path = tmp_path / 'repair_source.py'
    path.write_text('raise RuntimeError("private diagnostic")\n')
    module_name = 'zpp_plugin_repair_source'
    plugins._FILE_MODULES.pop(path.resolve(), None)
    try:
        with pytest.raises(RuntimeError):
            plugins.load_source('repair_source', tmp_path)
        assert module_name not in sys.modules
        assert path.resolve() not in plugins._FILE_MODULES
        path.write_text('''class Repaired:
    name = "repair_source"
    def fetch(self, item_meta, dest_dir):
        return None
source = Repaired()
''')
        plugin = plugins.load_source('repair_source', tmp_path)
        assert plugin.name == 'repair_source'
        assert sys.modules[module_name].source is plugin
    finally:
        plugins._FILE_MODULES.pop(path.resolve(), None)
        sys.modules.pop(module_name, None)


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    config_path = tmp_path / 'settings.toml'
    config_path.write_text('[zotero]\ncollection_key="C"\n[paths]\narchive_dir="archive"\n[fetch]\nsource_order=[]\n[plugins]\nenabled=["debug_source"]\n')
    cfg = load_config(config_path)
    cfg.plugins.directory = tmp_path / 'plugins'
    cfg.plugins.directory.mkdir()
    class Local:
        def __init__(self, *args): pass
        def items_without_attachment(self, key): return [PaperNeed('K', 'Title')]
        def creators_of(self, key): return []
        def close(self): pass
    monkeypatch.setattr(cli, 'ZoteroLocal', Local)
    store = Store(cfg.paths.state_db)
    yield cfg, store
    store.close()
    plugins._FILE_MODULES.pop((cfg.plugins.directory / 'debug_source.py').resolve(), None)
    sys.modules.pop('zpp_plugin_debug_source', None)


@pytest.mark.parametrize('level', [logging.INFO, logging.DEBUG])
def test_plugin_exception_traceback_is_opt_in_and_redacted(pipeline, caplog, capsys, level):
    cfg, store = pipeline
    (cfg.plugins.directory / 'debug_source.py').write_text('''class Source:
    name = "debug_source"
    def fetch(self, item_meta, dest_dir):
        raise ValueError("secret-token https://private.invalid/secret?token=secret-token")
source = Source()
''')
    caplog.set_level(level, logger='pipeline')
    assert cli.cmd_fetch(cfg, store, ['K'], plugin_names=['debug_source']) == 1
    output = caplog.text + capsys.readouterr().out + store.get('K')['note']
    assert '插件 debug_source 失败（ValueError）' in output
    assert 'secret-token' not in output and 'private.invalid' not in output
    assert ('调用栈' in output) == (level == logging.DEBUG)
    if level == logging.DEBUG:
        assert 'zpp_plugin_debug_source:4' in output
        assert str(cfg.plugins.directory) not in output
        assert 'raise ValueError' not in output


def test_none_result_diagnostic_identifies_plugin(pipeline, caplog):
    cfg, store = pipeline
    (cfg.plugins.directory / 'debug_source.py').write_text('''class Source:
    name = "debug_source"
    def fetch(self, item_meta, dest_dir): return None
source = Source()
''')
    assert cli.cmd_fetch(cfg, store, ['K'], plugin_names=['debug_source']) == 1
    assert '插件 debug_source 未返回候选' in caplog.text
    assert '插件 debug_source 未返回候选' in store.get('K')['note']


def test_verbose_flag_configures_debug_logging(pipeline, monkeypatch):
    cfg, _ = pipeline
    captured = {}
    monkeypatch.setattr(cli, 'load_config', lambda path: cfg)
    monkeypatch.setattr(cli.logging, 'basicConfig', lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(cli, 'cmd_fetch', lambda *args, **kwargs: 0)
    assert cli.main(['-v', 'fetch', '--only', 'K']) == 0
    assert captured['level'] == logging.DEBUG
