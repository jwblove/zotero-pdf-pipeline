"""Import once through MCP, then confirm that the parent still has a PDF.

Keys, names, paths and sizes are never compared with the import response. The
observed key is informational; file-manager add-ons own their attachment changes.
"""
from __future__ import annotations
import logging
import math
import re
import time
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from .config import DEFAULT_ATTACH_SETTLE_SECONDS, validate_attach_settle_seconds
from .models import AttachResult, PaperNeed
from .safe_files import copy_file, regular_file, safe_key
from .zotero_local import ZoteroLocal, ZoteroLocalError
from .zotero_mcp import McpClient, McpError

LOG = logging.getLogger("pipeline.attach")
SETTLE_INTERVAL_SECONDS = 2.0
SETTLE_REQUEST_TIMEOUT_SECONDS = 2.0

def to_windows_path(path: Path | str) -> str:
    """Normalize a WSL drive mount or absolute Windows path; reject other paths."""
    import ntpath
    import posixpath
    import re
    text = str(path)
    if '\x00' in text: raise ValueError('invalid path')
    if re.match(r'^[A-Za-z]:[\\/]', text):
        return ntpath.normpath(text)
    normalized = posixpath.normpath(text)
    match = re.fullmatch(r'/mnt/([A-Za-z])(?:/(.*))?', normalized)
    if not match: raise ValueError('attachment path must be on a WSL drive mount or an absolute Windows drive')
    rest = match.group(2) or ''
    if '\\' in rest: raise ValueError('invalid WSL path separator')
    return match.group(1).upper() + ':\\' + rest.replace('/', '\\')


@contextmanager
def _bounded_timeout(client: object, deadline: float | None):
    """Temporarily cap a real client's per-request timeout during settling."""
    if deadline is None or not hasattr(client, "timeout"):
        yield
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("settle deadline exceeded")
    original = client.timeout
    client.timeout = min(SETTLE_REQUEST_TIMEOUT_SECONDS, remaining)
    try:
        yield
    finally:
        client.timeout = original



def _data(item):
    if not isinstance(item, dict): return {}
    data = item.get('data', item)
    return data if isinstance(data, dict) else {}


def _is_pdf(item):
    data = _data(item)
    mime = str(data.get('contentType') or '').strip()
    if mime:
        return mime.split(';', 1)[0].strip().lower() == 'application/pdf'
    for field in ('filename', 'path', 'title'):
        value = str(data.get(field) or '').strip().lower()
        if value.startswith('attachments:'): value = value[len('attachments:'):].strip()
        if not value or value.startswith('//') or any(char in value for char in ('?', '#', '\x00')): continue
        # File names/paths only: URLs and their query strings are not evidence.
        if ':' in value and not re.match(r'^[a-z]:[\\/]', value): continue
        if value.endswith(('/', '\\')): continue
        if PureWindowsPath(value).suffix == '.pdf': return True
    return False


def _mcp_has_attachment(value, depth=0):
    """Parent read-back confirms attachment presence, never a particular key.

    Some MCP versions return only attachment keys, without MIME information.
    Local children supply PDF classification in that case. Explicit non-PDF
    MCP metadata cannot confirm a PDF; key changes between channels are fine.
    """
    if depth > 40: return False
    if isinstance(value, dict):
        attachments = value.get('attachments')
        if isinstance(attachments, list):
            for item in attachments:
                data = _data(item)
                if _is_pdf(item): return True
                if not str(data.get('contentType') or '').strip() and any(data.get(k) for k in ('key','itemKey','attachmentKey')):
                    return True
        if _data(value).get('itemType') == 'attachment' and _is_pdf(value): return True
        return any(_mcp_has_attachment(child, depth+1) for child in value.values())
    if isinstance(value, list):
        return any(_mcp_has_attachment(child, depth+1) for child in value)
    return False


class Attacher:
    def __init__(self, mcp: McpClient, local: ZoteroLocal, archive_dir: Path | None = None,
                 archive_copy: bool = False, library_id: int = 1,
                 attach_settle_seconds: float = DEFAULT_ATTACH_SETTLE_SECONDS):
        self.mcp, self.local = mcp, local
        self.archive_dir, self.archive_copy = archive_dir, archive_copy
        self.library_id = library_id
        self.attach_settle_seconds = validate_attach_settle_seconds(attach_settle_seconds)

    def attach(self, paper: PaperNeed, pdf_path: Path, title: str | None = None) -> AttachResult:
        """Import once; imported files wait the window, linked PDFs may finish early.

        Normal pipeline selection targets parents with no attachments. If called
        on a parent that already has a PDF, successful import plus continued PDF
        presence is sufficient; no new key or increase in count is required.
        """
        safe_key(paper.item_key)
        if not regular_file(pdf_path, 1024**3):
            return AttachResult(paper.item_key, False, detail='待导入 PDF 缺失或不是普通文件')
        try:
            win_path = to_windows_path(pdf_path)
        except ValueError:
            return AttachResult(paper.item_key, False, detail='附件路径须为 /mnt/<drive>/ 下的路径或绝对 Windows 路径')
        try:
            had_pdf = any(_is_pdf(item) for item in self.local.attachments_of(paper.item_key))
        except ZoteroLocalError:
            return AttachResult(paper.item_key, False, detail='无法读取导入前的附件')
        try:
            response = self.mcp.call_tool('write_item', {'action':'import', 'filePath':win_path,
                'parentItemKey':paper.item_key, 'title':title or pdf_path.name})
        except McpError:
            return AttachResult(paper.item_key, False, detail='MCP 导入失败（McpError）')
        if isinstance(response, dict) and response.get('success') is False:
            return AttachResult(paper.item_key, False, detail='MCP 导入返回失败')

        def read_back(deadline):
            try:
                local_client = self.local if hasattr(self.local, 'timeout') else getattr(self.local, '_client', self.local)
                with _bounded_timeout(local_client, deadline):
                    pdfs = [item for item in self.local.attachments_of(paper.item_key) if _is_pdf(item)]
                with _bounded_timeout(self.mcp, deadline):
                    details = self.mcp.call_tool('get_item_details', {'itemKey':paper.item_key,
                        'libraryID':self.library_id, 'mode':'complete'})
                return pdfs, _mcp_has_attachment(details)
            except (ZoteroLocalError, McpError, TimeoutError):
                return None

        deadline = time.monotonic() + self.attach_settle_seconds
        previous_linked = False
        final = None
        # Only PDF presence is stable: changes to keys/names never reset this.
        for _ in range(math.ceil(self.attach_settle_seconds / SETTLE_INTERVAL_SECONDS)):
            if time.monotonic() >= deadline: break
            snapshot = read_back(deadline)
            linked = bool(snapshot and snapshot[1] and any(
                _data(item).get('linkMode') == 'linked_file' for item in snapshot[0]))
            if linked and previous_linked:
                final = snapshot
                break
            previous_linked = linked
            remaining = deadline-time.monotonic()
            if remaining > 0: time.sleep(min(SETTLE_INTERVAL_SECONDS, remaining))
        if final is None:
            # One fresh end-of-window check, no extra settling/grace window.
            # Only this read's network latency can extend the wait (<= 2 s).
            final = read_back(time.monotonic() + SETTLE_REQUEST_TIMEOUT_SECONDS)
        if final is None:
            return AttachResult(paper.item_key, False, detail='等待结束后无法回读确认 PDF，需人工检查；未重新导入')
        pdfs, mcp_confirmed = final
        if not pdfs:
            return AttachResult(paper.item_key, False, detail='等待结束后父条目没有 PDF 附件；未重新导入')
        if not mcp_confirmed:
            return AttachResult(paper.item_key, False, detail='等待结束后 MCP 回读未确认 PDF 附件，需人工检查；未重新导入')
        key = str(_data(pdfs[0]).get('key') or pdfs[0].get('key') or '')
        detail = '已挂载：父条目仍有 PDF 附件；附件 key 仅供参考，之后可能变化'
        if had_pdf: detail += '（导入前已有 PDF，按导入成功后仍有 PDF 确认）'
        # Optional copy of our original download only; never locate or change an
        # attachment managed by Zotero or another add-on.
        if self.archive_copy and self.archive_dir and pdf_path.exists():
            try:
                copy_file(pdf_path, self.archive_dir, pdf_path.name, max_bytes=1024**3)
            except (OSError, ValueError) as exc:
                LOG.warning('原始下载文件归档失败（%s）', type(exc).__name__)
        LOG.info('挂载成功 [%s] 附件 key=%s（仅供参考）', paper.item_key, key)
        return AttachResult(paper.item_key, True, key, detail, True)

    def verify_all(self, papers: list[PaperNeed]) -> dict[str, bool]:
        results = {}
        for paper in papers:
            try: results[paper.item_key] = any(_is_pdf(item) for item in self.local.attachments_of(paper.item_key))
            except ZoteroLocalError: results[paper.item_key] = False
        return results
