"""Native Windows smoke tests for the published setup and normal CLI paths."""
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='uses native Windows kernel objects')


def test_private_plugin_state_and_profile(tmp_path, monkeypatch):
    import sys
    from zotero_pdf_pipeline.plugins import load_source
    from zotero_pdf_pipeline.windows_files import private_path

    plugin = load_source('ablesci', Path(__file__).resolve().parents[1] / 'plugins')
    module = sys.modules[type(plugin).__module__]
    monkeypatch.setattr(module, '_STATE_PATH', tmp_path / 'state.json')
    module._write_state({'request': 'synthetic'})
    assert module._read_state() == {'request': 'synthetic'}
    profile = tmp_path / 'profile'
    profile.mkdir()
    private_path(profile, directory=True)


def test_exclusive_binary_files(tmp_path):
    from zotero_pdf_pipeline.safe_files import write_bytes, copy_file, UnsafePath

    payload = b'%PDF-1.5\r\n\x1a\x00'
    first = write_bytes(tmp_path, 'sample.pdf', payload)
    second = copy_file(first, tmp_path, first.name)
    assert first != second and first.read_bytes() == second.read_bytes() == payload
    for name in ('CON.pdf', 'file.pdf:stream'):
        with pytest.raises(UnsafePath):
            write_bytes(tmp_path, name, payload)


def test_inbox_cli_with_native_pdf_worker(tmp_path, monkeypatch):
    from pypdf import PdfWriter
    from zotero_pdf_pipeline import cli
    from zotero_pdf_pipeline.models import PaperNeed

    title = 'Synthetic study of optical absorption'
    class Local:
        def __init__(self, *args): pass
        def items_without_attachment(self, key): return [PaperNeed('ITEM', title, authors=['Smith'])]
        def collection_name(self, key): return 'Smoke test'
        def creators_of(self, key): return ['Smith']
        def close(self): pass

    monkeypatch.setattr(cli, 'ZoteroLocal', Local)
    config = tmp_path / 'config.toml'
    config.write_text('[zotero]\ncollection_key="TEST"\n[paths]\narchive_dir="archive"\n'
                      '[fetch]\nsource_order=["inbox"]\nmin_pdf_bytes=1\n')
    assert cli.main(['--config', str(config), 'scan']) == 0
    inbox = tmp_path / 'data' / 'inbox' / (title + '.pdf')
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with inbox.open('wb') as stream:
        writer.write(stream)
    assert cli.main(['--config', str(config), 'fetch', '--free-only', '--only', 'ITEM']) == 0
    archive, = (tmp_path / 'archive').glob('*.pdf')
    assert archive.read_bytes() == inbox.read_bytes()
