"""PDF presence after one import; keys and file-manager changes are informational."""
import pytest
from zotero_pdf_pipeline import cli
from zotero_pdf_pipeline.attach import Attacher
from zotero_pdf_pipeline.config import ConfigError, FetchConfig, load_config
from zotero_pdf_pipeline.models import AttachResult, PaperNeed, Stage
from zotero_pdf_pipeline.state import Store
from zotero_pdf_pipeline.zotero_mcp import McpError


def item(key, mode='imported_file', **fields):
    return {'data': {'key': key, 'contentType': 'application/pdf', 'linkMode': mode, **fields}}


class Clock:
    def __init__(self): self.now = 0.0
    def sleep(self, seconds):
        assert seconds >= 0
        self.now += seconds
    def monotonic(self): return self.now


class Local:
    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)
        self.current = []
    def attachments_of(self, key):
        self.current = next(self.snapshots, self.current)
        return self.current


class Mcp:
    def __init__(self, local): self.local, self.imports = local, 0
    def call_tool(self, name, args):
        if name == 'write_item':
            self.imports += 1
            assert args['action'] == 'import'
            return {'attachmentKey': 'IMPORT_RESULT_KEY'}
        assert name == 'get_item_details' and args['itemKey'] == 'PARENT'
        return {'attachments': [{'key': x['data']['key']} for x in self.local.current]}


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr('zotero_pdf_pipeline.attach.time.sleep', clock.sleep)
    monkeypatch.setattr('zotero_pdf_pipeline.attach.time.monotonic', clock.monotonic)
    return clock


@pytest.fixture
def pdf(tmp_path, clock, monkeypatch):
    monkeypatch.setattr('zotero_pdf_pipeline.attach.to_windows_path', lambda p: 'C:\\test\\' + p.name)
    path = tmp_path / 'paper.pdf'
    path.write_bytes(b'%PDF-test-data')
    return path


def perform(pdf, snapshots, seconds=4, mcp_class=Mcp):
    local = Local(snapshots)
    mcp = mcp_class(local)
    result = Attacher(mcp, local, attach_settle_seconds=seconds).attach(PaperNeed('PARENT', 'Title'), pdf)
    assert mcp.imports == 1
    return result


def test_stable_imported_pdf_waits_full_window(pdf, clock):
    result = perform(pdf, [[], [item('K1')]])
    assert result.ok and result.verified and result.attachment_key == 'K1'
    assert clock.now == 4
    assert '仅供参考' in result.detail


def test_measured_replacement_ignores_renamed_title_empty_filename_and_missing_size(pdf, clock):
    imported = item('K1', title='Huang等 - paper.pdf')
    linked = item('K2', 'linked_file', title='Huang 等 - renamed paper.pdf', filename='')
    class Timeline(Local):
        def attachments_of(self, key):
            self.current = [] if clock.now < 1 else [imported] if clock.now < 7 else [linked]
            return self.current
    local = Timeline([])
    mcp = Mcp(local)
    result = Attacher(mcp, local).attach(PaperNeed('PARENT', 'Title'), pdf)
    assert result.ok and result.verified and result.attachment_key == 'K2'
    assert 9 <= clock.now < 20 and mcp.imports == 1


def test_linked_pdf_two_reads_finish_early(pdf, clock):
    result = perform(pdf, [[], [item('K2', 'linked_file')]], seconds=20)
    assert result.ok and result.attachment_key == 'K2' and clock.now == 2


def test_key_name_path_and_count_changes_do_not_reset_pdf_presence(pdf, clock):
    result = perform(pdf, [[], [item('K2', 'linked_file', filename='one.pdf')],
        [item('K3', 'linked_file', path='attachments:other.pdf'), item('K4', 'linked_file')]], seconds=20)
    assert result.ok and result.attachment_key == 'K3' and clock.now == 2


def test_no_pdf_at_window_end_fails_without_reimport(pdf, clock):
    result = perform(pdf, [[], [item('K1')], []])
    assert not result.ok and not result.verified and not result.attachment_key
    assert '没有 PDF 附件' in result.detail and clock.now == 4


def test_empty_gap_does_not_fail_before_replacement(pdf, clock):
    result = perform(pdf, [[], [item('K1')], [], [item('K2')]])
    assert result.ok and result.attachment_key == 'K2' and clock.now == 4


def test_imported_key_can_change_on_every_poll(pdf, clock):
    result = perform(pdf, [[], [item('K1')], [item('K2')], [item('K3')]])
    assert result.ok and result.attachment_key == 'K3' and clock.now == 4


def test_existing_pdf_need_not_have_new_key_or_increased_count(pdf, clock):
    existing = item('EXISTING')
    result = perform(pdf, [[existing]])
    assert result.ok and result.attachment_key == 'EXISTING'
    assert '导入前已有 PDF' in result.detail and clock.now == 4


@pytest.mark.parametrize('field', ['filename', 'path', 'title'])
def test_pdf_extension_can_identify_attachment_without_mime(pdf, clock, field):
    linked = item('K2', 'linked_file', **{field:'renamed.PDF'})
    linked['data'].pop('contentType')
    assert perform(pdf, [[], [linked]]).ok


def test_non_pdf_children_do_not_confirm_attachment(pdf, clock):
    non_pdf = {'data': {'key':'HTML', 'contentType':'text/html', 'title':'Landing page'}}
    assert not perform(pdf, [[], [non_pdf]]).ok and clock.now == 4


def test_import_mcp_error_fails_without_polling_or_retry(pdf, clock):
    class FailedMcp(Mcp):
        def call_tool(self, name, args):
            assert name == 'write_item'
            self.imports += 1
            raise McpError('MCP unavailable')
    result = perform(pdf, [[]], mcp_class=FailedMcp)
    assert not result.ok and not result.verified and clock.now == 0


def test_import_reported_failure_fails(pdf, clock):
    class FailedMcp(Mcp):
        def call_tool(self, name, args): self.imports += 1; return {'success':False}
    assert not perform(pdf, [[]], mcp_class=FailedMcp).ok and clock.now == 0


def test_mcp_parent_crosscheck_required(pdf, clock):
    class EmptyMcp(Mcp):
        def call_tool(self, name, args):
            if name == 'write_item': return super().call_tool(name, args)
            return {'attachments':[]}
    result = perform(pdf, [[], [item('K1')]], mcp_class=EmptyMcp)
    assert not result.ok and 'MCP' in result.detail and clock.now == 4


def test_changed_key_between_local_and_mcp_reads_is_not_a_failure(pdf):
    class ChangedMcp(Mcp):
        def call_tool(self, name, args):
            if name == 'write_item': return super().call_tool(name, args)
            return {'attachments':[{'key':'NEWER_KEY'}]}
    assert perform(pdf, [[], [item('K2', 'linked_file')]], mcp_class=ChangedMcp).ok


def test_source_moved_by_addon_does_not_require_reading_it_again(pdf):
    class MovingMcp(Mcp):
        def call_tool(self, name, args):
            if name == 'write_item': pdf.unlink()
            return super().call_tool(name, args)
    result = perform(pdf, [[], [item('K2', 'linked_file', filename='other.pdf')]], mcp_class=MovingMcp)
    assert result.ok and result.attachment_key == 'K2' and not pdf.exists()


def test_read_timeouts_capped_and_restored(pdf):
    local = Local([[], [item('K2', 'linked_file')]])
    local.timeout = 30
    mcp = Mcp(local); mcp.timeout = 120
    observed = []
    read = local.attachments_of
    call = mcp.call_tool
    def read_local(key):
        observed.append(local.timeout)
        return read(key)
    def read_mcp(name, args):
        if name == 'get_item_details': observed.append(mcp.timeout)
        return call(name, args)
    local.attachments_of, mcp.call_tool = read_local, read_mcp
    assert Attacher(mcp, local).attach(PaperNeed('PARENT','Title'), pdf).ok
    assert all(0 < timeout <= 2 for timeout in observed[1:])
    assert local.timeout == 30 and mcp.timeout == 120


def test_cli_records_informational_key_and_failed_stage(monkeypatch, tmp_path, capsys):
    path = tmp_path/'settings.toml'
    path.write_text('[zotero]\ncollection_key="C"\n[paths]\narchive_dir="archive"\n[fetch]\nattach_settle_seconds=3.5\n')
    cfg = load_config(path)
    class FakeLocal:
        def __init__(self,*a): pass
        def close(self): pass
    results = iter([AttachResult('K', True, 'FINAL', '附件 key 仅供参考', True),
                    AttachResult('K', False, detail='等待结束后父条目没有 PDF 附件；未重新导入')])
    calls = []
    class FakeAttacher:
        def __init__(self,*a,**k): assert k['attach_settle_seconds'] == 3.5
        def attach(self,*a,**k): calls.append(True); return next(results)
    monkeypatch.setattr(cli,'ZoteroLocal',FakeLocal)
    monkeypatch.setattr(cli,'McpClient',lambda *a:object())
    monkeypatch.setattr(cli,'Attacher',FakeAttacher)
    monkeypatch.setattr(cli,'check_pdf',lambda *a,**k:type('Check',(),{'ok':True})())
    file = cfg.paths.staging_dir/'paper.pdf'; file.write_bytes(b'%PDF-test')
    store = Store(cfg.paths.state_db)
    try:
        store.upsert_need(PaperNeed('K','Title'))
        store.mark('K',Stage.DOWNLOADED,file_path=str(file),attachment_key='STALE')
        assert cli.cmd_attach(cfg,store,['K']) == 0
        assert store.get('K')['stage'] == 'attached' and store.get('K')['attachment_key'] == 'FINAL'
        assert '仅供参考' in capsys.readouterr().out
        store.mark('K',Stage.DOWNLOADED)
        assert cli.cmd_attach(cfg,store,['K']) == 1 and store.get('K')['stage'] == 'failed'
        assert store.get('K')['attachment_key'] == ''
        assert len(calls) == 2  # One attempt in each explicitly requested command.
    finally: store.close()


@pytest.mark.parametrize('value',[0,-1,61,float('inf'),float('nan'),True])
def test_settle_config_rejects_out_of_range_values(tmp_path,value):
    path=tmp_path/'settings.toml'
    path.write_text('[paths]\narchive_dir="archive"\n[fetch]\nattach_settle_seconds='+str(value).lower()+'\n')
    with pytest.raises(ConfigError,match='attach_settle_seconds'):load_config(path)
    with pytest.raises(ConfigError,match='attach_settle_seconds'):Attacher(object(),object(),attach_settle_seconds=value)


def test_settle_config_default_and_hard_max(tmp_path):
    assert FetchConfig().attach_settle_seconds == 20
    path=tmp_path/'settings.toml'
    path.write_text('[paths]\narchive_dir="archive"\n[fetch]\nattach_settle_seconds=60\n')
    assert load_config(path).fetch.attach_settle_seconds == 60
    assert Attacher(object(),object(),attach_settle_seconds=60).attach_settle_seconds == 60

def test_pdf_disappearing_only_at_deadline_fails_fresh_end_check(pdf,clock):
    class AtDeadline(Local):
        def __init__(self):super().__init__([]);self.before=True
        def attachments_of(self,key):
            if self.before:self.before=False;self.current=[]
            else:self.current=[item('K1')] if clock.now < 4 else []
            return self.current
    local=AtDeadline();mcp=Mcp(local)
    result=Attacher(mcp,local,attach_settle_seconds=4).attach(PaperNeed('PARENT','Title'),pdf)
    assert not result.ok and '没有 PDF 附件' in result.detail
    assert clock.now == 4 and mcp.imports == 1


def test_readback_error_waits_full_window_and_never_reimports(pdf,clock):
    class UnavailableMcp(Mcp):
        def call_tool(self,name,args):
            if name=='write_item':return super().call_tool(name,args)
            raise McpError('MCP unavailable')
    result=perform(pdf,[[],[item('K1')]],mcp_class=UnavailableMcp)
    assert not result.ok and not result.verified and clock.now == 4
    assert '无法回读确认' in result.detail and '未重新导入' in result.detail
