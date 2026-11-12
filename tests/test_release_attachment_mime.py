"""Release PDF classification regressions; parent presence owns completion."""
import pytest
from zotero_pdf_pipeline.attach import Attacher, _is_pdf, _mcp_has_attachment
from zotero_pdf_pipeline.models import PaperNeed


def metadata(**fields): return {'data': {'key':'HTML', **fields}}


@pytest.mark.parametrize('mime', ['text/html', ' TEXT/HTML ; charset=utf-8', 'application/octet-stream'])
@pytest.mark.parametrize('field,value', [('title','paper.pdf - publisher page'), ('filename','paper.pdf'), ('path','attachments:paper.pdf')])
def test_explicit_non_pdf_mime_wins_in_both_channels(mime, field, value):
    attachment = metadata(contentType=mime, **{field:value})
    assert not _is_pdf(attachment)
    assert not _mcp_has_attachment({'attachments':[attachment]})
    assert not _mcp_has_attachment({'data':{'itemType':'attachment', **attachment['data']}})


@pytest.mark.parametrize('value', ['paper.pdf - publisher page', 'https://host.invalid/page?download=paper.pdf',
    'https://host.invalid/paper.pdf', '//host.invalid/files/paper.pdf', 'paper.pdf?download=1', 'paper.pdf#section', 'attachments:https://host.invalid/a.pdf'])
@pytest.mark.parametrize('field', ['title', 'filename', 'path'])
def test_missing_mime_requires_file_suffix_not_url_or_substring(value, field):
    assert not _is_pdf(metadata(**{field:value}))


@pytest.mark.parametrize('value', [' paper.PDF ', 'attachments:paper.PDF', '/folder/a.Pdf', r'C:\folder\a.pdf'])
@pytest.mark.parametrize('field', ['title', 'filename', 'path'])
def test_missing_mime_real_filename_suffix(value, field):
    assert _is_pdf(metadata(contentType='  ', **{field:value}))


def test_pdf_mime_ignores_names():
    assert _is_pdf(metadata(contentType=' Application/PDF ; name=x', title='renamed publisher page'))


@pytest.mark.parametrize('mime,expected', [('text/html',False), ('application/pdf',True)])
def test_full_attacher_waits_then_classifies_current_parent(monkeypatch, tmp_path, mime, expected):
    now = [0.0]
    monkeypatch.setattr('zotero_pdf_pipeline.attach.time.monotonic', lambda:now[0])
    monkeypatch.setattr('zotero_pdf_pipeline.attach.time.sleep', lambda s:now.__setitem__(0,now[0]+s))
    monkeypatch.setattr('zotero_pdf_pipeline.attach.to_windows_path', lambda p:r'C:\paper.pdf')
    pdf = tmp_path/'paper.pdf'; pdf.write_bytes(b'%PDF-test')
    class Local:
        first = True
        def attachments_of(self, key):
            if self.first:
                self.first = False
                return []
            return [metadata(key='RENAMED_KEY', contentType=mime, title='paper.pdf - publisher page', path='moved')]
    class Mcp:
        imports = 0
        def call_tool(self, name, args):
            if name == 'write_item':
                self.imports += 1
                return {'success':True,'attachmentKey':'OLD_KEY'}
            return {'attachments':[metadata(key='ANOTHER_KEY',contentType=mime,title='paper.pdf - publisher page')]}
    mcp = Mcp()
    result = Attacher(mcp,Local(),attach_settle_seconds=4).attach(PaperNeed('PARENT','Title'),pdf)
    assert result.ok is expected and result.verified is expected
    assert mcp.imports == 1 and now[0] == 4
    if expected: assert result.attachment_key == 'RENAMED_KEY'
