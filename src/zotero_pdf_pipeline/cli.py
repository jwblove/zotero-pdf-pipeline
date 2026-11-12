from __future__ import annotations

import argparse
import logging
import json
import html
import re
import os
import sys
from pathlib import Path

# 让脚本能以 `python src/cli.py` 方式直接运行（把 src 加入模块搜索路径）


from .attach import Attacher
from .config import Config, ConfigError, load_config
from .free_sources import try_all as try_free_sources, lookup_crossref, FreeSourceError
from dataclasses import replace
from .models import AttachResult, PaperNeed, ResolvedFile, Source, Stage
from .naming import build_filename, unique_path
from .state import Store, RunLock, RunLockedError
from .safe_files import copy_file, write_bytes, safe_key
from .public_http import proxy_policy
from .validate import check_pdf
from .quarantine import quarantine_pdf
from .zotero_local import ZoteroLocal, ZoteroLocalError
from .zotero_mcp import McpClient, McpError

LOG = logging.getLogger("pipeline")

def setup_logging(verbose: bool = False) -> None:
    """配置统一日志格式。

    在全局中的作用:
        所有模块通过 logging 输出；此函数只调用一次（cli 启动时）。

    Args:
        verbose: True 时输出 DEBUG 级日志
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )
    # ---------- 降噪 ----------
    # 目的: httpx 每个请求都打一行 INFO，会把关键流程日志淹没；
    #       只保留 WARNING 以上，避免用户看不到真正重要的输出
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def cmd_doctor(cfg: Config) -> int:
    """环境自检：验证各依赖通道是否可用。

    在全局中的作用:
        上线前先跑它，避免在长流程中途才发现 Zotero/MCP/凭据不可用。

    实现效果:
        逐项打印 ✅/❌ 与修复建议，并返回退出码（全部通过为 0）。

    Args:
        cfg: 配置对象

    Returns:
        0 表示全部通过；1 表示存在失败项
    """
    print("=" * 68)
    print("Zotero 文献流水线 · 环境自检")
    print("=" * 68)
    problems: list[str] = []

    # ---------- 检查 1: Zotero 本地 API ----------
    local = ZoteroLocal(cfg.zotero.local_api)
    if local.ping():
        name = local.collection_name(cfg.zotero.collection_key)
        print(f"✅ Zotero 本地 API 可用（集合：{name}）")
    else:
        print("❌ Zotero 本地 API 不可用")
        problems.append(
            "启动 Zotero 桌面端，并在 设置 → 高级 中勾选『允许其他应用与 Zotero 通信』"
        )

    # ---------- 检查 2: zotero-mcp ----------
    mcp = McpClient(cfg.zotero.mcp_url)
    try:
        mcp.initialize()
        libs = mcp.call_tool("get_libraries", {})
        count = len(libs) if isinstance(libs, list) else 0
        print(f"✅ zotero-mcp 可用（文库数：{count}）")
    except McpError as exc:
        print(f"❌ zotero-mcp 不可用：{str(exc)[:120]}")
        problems.append("确认 Zotero 的 zotero-mcp 插件正在运行、端口 23120 可访问")

    # ---------- 检查 3: 归档目录 ----------
    if cfg.paths.archive_dir.exists():
        n = len(list(cfg.paths.archive_dir.glob("*.pdf")))
        print(f"✅ 归档目录存在（已有 {n} 个 PDF）")
    else:
        print(f"❌ 归档目录不存在：{cfg.paths.archive_dir}")
        problems.append("检查 config.toml 的 [paths].archive_dir")

    # ---------- 检查 4: pypdf（内容校验用）----------
    try:
        import pypdf  # noqa: F401

        print("✅ pypdf 已安装（可做标题相似度校验）")
    except ImportError:
        print("❌ 未安装 pypdf：无法验证 PDF，fetch/attach 会拒绝文件；请安装项目依赖")
        problems.append("安装必需依赖 pypdf 后重试")

    # ---------- 检查 6: 待处理条目统计 ----------
    if local.ping():
        try:
            needs = local.items_without_attachment(cfg.zotero.collection_key)
            print(f"✅ 扫描到缺附件文献条目：{len(needs)} 篇")
        except ZoteroLocalError as exc:
            print(f"⚠️  扫描失败：{str(exc)[:100]}")

    local.close()
    print("-" * 68)
    if problems:
        print("需要处理：")
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
        return 1
    print("全部检查通过 ✅")
    return 0


# ======================================================================
# 子命令：scan（只读扫描）
# ======================================================================

def cmd_scan(cfg: Config, store: Store) -> int:
    """扫描集合并登记缺附件条目（只读，不下载）。

    在全局中的作用:
        让用户先看到"到底缺哪些"，确认无误后再执行下载。

    实现效果:
        把每个缺附件条目写入状态库（新条目为 pending），并打印清单。

    Args:
        cfg: 配置
        store: 状态库

    Returns:
        退出码（0 成功）
    """
    local = ZoteroLocal(cfg.zotero.local_api)
    try:
        needs = local.items_without_attachment(cfg.zotero.collection_key)
    except ZoteroLocalError as exc:
        LOG.error("%s", exc)
        return 1

    print("=" * 68)
    print(f"集合：{local.collection_name(cfg.zotero.collection_key)}")
    print(f"缺附件文献：{len(needs)} 篇")
    print("=" * 68)

    for i, paper in enumerate(needs, 1):
        # ---------- 登记到状态库 ----------
        # 目的: 保底记录，便于后续 fetch 直接读库而不必重新扫描
        store.upsert_need(paper)
        stage = store.get_stage(paper.item_key)
        print(f"{i:2d}. [{paper.item_key}] {paper.year or '----'} {paper.publication[:28]}")
        print(f"    {paper.title[:96]}")
        print(f"    DOI: {paper.doi or '(无)'}   状态: {stage}")
        # 顺带取作者，供命名使用
        local.creators_of(paper.item_key)

    print("-" * 68)
    print(f"已写入状态库：{cfg.paths.state_db}")
    local.close()
    return 0


# ======================================================================
# 子命令：fetch（下载）
# ======================================================================

def cmd_fetch(cfg: Config, store: Store, only_keys: list[str], free_only: bool = False,
              plugin_names: list[str] | None = None) -> int:
    with proxy_policy(cfg.fetch.use_env_proxy):
        return _cmd_fetch(cfg, store, only_keys, free_only, plugin_names)


def _cmd_fetch(cfg: Config, store: Store, only_keys: list[str], free_only: bool = False,
               plugin_names: list[str] | None = None) -> int:
    if not only_keys:
        raise ValueError("--only is required")
    plugin_names = plugin_names or []
    if free_only and plugin_names:
        raise ValueError("--free-only excludes plugins")
    if any(name not in cfg.plugins.enabled for name in plugin_names):
        LOG.error("指定的插件未在配置中启用")
        return 2
    local = ZoteroLocal(cfg.zotero.local_api)
    try:
        needs = local.items_without_attachment(cfg.zotero.collection_key)
    except ZoteroLocalError as exc:
        LOG.error("%s", exc)
        return 1

    # ---------- 步骤 1: 过滤出本次要处理的条目 ----------
    # 目的: 支持 --only 定向处理；并跳过状态库中已完成的条目（幂等）
    todo: list[PaperNeed] = []
    for paper in needs:
        if only_keys and paper.item_key not in only_keys:
            continue
        store.upsert_need(paper)
        if not store.needs_work(paper.item_key):
            LOG.info("跳过已完成条目 [%s]", paper.item_key)
            continue
        todo.append(paper)

    if not todo:
        print("没有需要下载的条目。")
        local.close()
        return 0

    print(f"待下载：{len(todo)} 篇")

    failed = 0
    for i, paper in enumerate(todo, 1):
        print("-" * 68)
        print(f"[{i}/{len(todo)}] {paper.title[:80]}")
        authors = paper.authors or local.creators_of(paper.item_key)
        previous = store.get(paper.item_key)
        if previous and previous['stage'] == Stage.DOWNLOADED.value and previous.get('file_path'):
            cached = check_pdf(Path(previous['file_path']), expected_title=paper.title, expected_doi=paper.doi,
                min_bytes=cfg.fetch.min_pdf_bytes, similarity_threshold=cfg.fetch.title_similarity,
                expected_authors=authors, max_bytes=cfg.fetch.max_pdf_bytes,
                institutional_authors=paper.has_institutional_authors,
                inbox_matched=previous.get('source') == 'inbox' and previous.get('inbox_matched') == '1',
                allow_scanned=previous.get('source') == 'inbox' and previous.get('inbox_matched') == '1')
            if cached.ok:
                LOG.info('[%s] 复用已验证的下载文件，等待 attach', paper.item_key)
                continue
            try:
                rejected = quarantine_pdf(Path(previous['file_path']), cfg.paths.staging_dir, paper.item_key,
                                          max_bytes=cfg.fetch.max_pdf_bytes)
            except (OSError, ValueError):
                rejected = Path(previous['file_path'])
            store.mark(paper.item_key, Stage.PENDING, file_path=str(rejected),
                       note='缓存文件验证失败，尝试重新获取')

        resolved: ResolvedFile | None = None
        free_diagnostics: list[str] = []

        lookup_paper = paper
        if cfg.fetch.enable_crossref and not paper.doi:
            try:
                lookup_paper = replace(paper, authors=paper.authors or authors)
                found_doi = lookup_crossref(lookup_paper, cfg.fetch.http_timeout,
                                            cfg.fetch.title_similarity, cfg.contact_email)
                lookup_paper = replace(lookup_paper, doi=found_doi)
            except FreeSourceError as exc:
                free_diagnostics.append(str(exc))
            except Exception as exc:
                free_diagnostics.append(f"Crossref 查询失败（{type(exc).__name__}）")
        resolved = try_free_sources(
            lookup_paper, cfg.paths.staging_dir, cfg.fetch.source_order,
            cfg.fetch.enable_arxiv, cfg.fetch.http_timeout, diagnostics=free_diagnostics,
            contact_email=cfg.contact_email, inbox_dir=cfg.paths.inbox_dir,
            title_threshold=cfg.fetch.title_similarity, allow_credentials=not free_only, max_pdf_bytes=cfg.fetch.max_pdf_bytes)
        if resolved is None and not free_only and plugin_names:
            from .plugins import fetch_plugin
            for name in plugin_names:
                try:
                    resolved = fetch_plugin(name, cfg.plugins.directory, lookup_paper, cfg.paths.staging_dir,
                                            max_pdf_bytes=cfg.fetch.max_pdf_bytes)
                except Exception as exc:
                    diagnostic = f"插件 {name} 失败（{type(exc).__name__}）"
                    free_diagnostics.append(diagnostic)
                    LOG.warning("%s", diagnostic)
                    if LOG.isEnabledFor(logging.DEBUG):
                        frames = []
                        tb = exc.__traceback__
                        while tb is not None:
                            module = tb.tb_frame.f_globals.get("__name__", "")
                            if not isinstance(module, str) or not all(part.isidentifier() for part in module.split(".")):
                                module = "<unknown>"
                            frames.append(f"{module}:{tb.tb_lineno}")
                            tb = tb.tb_next
                        LOG.debug("插件 %s 调用栈（%s）：%s", name, type(exc).__name__, " → ".join(frames))
                    continue
                if resolved is not None:
                    break
                diagnostic = f"插件 {name} 未返回候选"
                free_diagnostics.append(diagnostic)
                LOG.warning("%s", diagnostic)
        if resolved is None:
            note = "未取得 PDF：" + "；".join(free_diagnostics)
            LOG.error("[%s] %s", paper.item_key, note)
            store.mark(paper.item_key, Stage.FAILED, note=note, bump_attempts=True)
            failed += 1
            continue

        # ---------- 步骤 5: 内容校验 ----------
        # 目的: 确认 PDF 真的是目标论文，避免污染 Zotero
        result = check_pdf(
            resolved.file_path,
            expected_title=paper.title,
            expected_doi=lookup_paper.doi,
            expected_authors=authors, max_bytes=cfg.fetch.max_pdf_bytes,
            institutional_authors=paper.has_institutional_authors,
            inbox_matched=resolved.source == Source.INBOX and resolved.inbox_matched,
            allow_scanned=resolved.source == Source.INBOX and resolved.inbox_matched,
            min_bytes=cfg.fetch.min_pdf_bytes,
            similarity_threshold=cfg.fetch.title_similarity,
        )
        if not result.ok:
            LOG.error("校验未通过：%s", result.reason)
            try:
                resolved.file_path = quarantine_pdf(resolved.file_path, cfg.paths.staging_dir, paper.item_key,
                                                    max_bytes=cfg.fetch.max_pdf_bytes)
            except (OSError, ValueError) as exc:
                LOG.error("隔离失败（不挂载）：%s", type(exc).__name__)
            store.mark(
                paper.item_key,
                Stage.FAILED,
                source=(resolved.source.value if isinstance(resolved.source, Source) else resolved.source),
                license=resolved.license, oa_status=resolved.oa_status,
                file_path=str(resolved.file_path),
                note=f"校验失败：{result.reason}",
                bump_attempts=True,
            )
            failed += 1
            continue

        if '人工核对元数据' in getattr(result, 'reason', ''):
            LOG.warning('[%s] %s', paper.item_key, result.reason)

        # ---------- 步骤 6: 规范命名并归档到归档目录 ----------
        # 目的: 让磁盘文件名与 Zotero 的命名规范一致，
        #       避免 附件管理插件 二次改名产生 " 1" 冲突副本
        final_name = build_filename(authors, paper.year, paper.title)
        archive_path = resolved.file_path
        try:
            archive_path = copy_file(resolved.file_path, cfg.paths.archive_dir, final_name,
                                     max_bytes=cfg.fetch.max_pdf_bytes)
            LOG.info("已归档：%s", archive_path.name)
        except (OSError, ValueError) as exc:
            LOG.warning("归档失败（保留已验证暂存文件）：%s", type(exc).__name__)

        store.mark(
            paper.item_key,
            Stage.DOWNLOADED,
            source=(resolved.source.value if isinstance(resolved.source, Source) else resolved.source),
            license=resolved.license, oa_status=resolved.oa_status,
            file_path=str(archive_path),
            inbox_matched=resolved.inbox_matched, authors=authors,
            note=f"{result.reason if hasattr(result,'reason') else ''} 页数={result.pages} 相似度={result.similarity:.2f} 来源={(resolved.source.value if isinstance(resolved.source, Source) else resolved.source)}",
        )
        print(f"  ✅ 已获取：{archive_path.name}  （来源：{(resolved.source.value if isinstance(resolved.source, Source) else resolved.source)}）")

    print("=" * 68)
    print(f"下载完成：成功 {len(todo) - failed}，失败 {failed}")
    local.close()
    return 1 if failed else 0


# ======================================================================
# 子命令：attach（挂载）
# ======================================================================

def cmd_attach(cfg: Config, store: Store, only_keys: list[str] | None) -> int:
    """把已下载的 PDF 挂载到 Zotero 条目。

    在全局中的作用:
        流水线的"入库"阶段；与 fetch 分开，便于先检查下载质量再挂载。

    实现效果:
        取出状态为 downloaded 的记录，逐个挂载并回读验证；成功记 attached。

    Args:
        cfg: 配置
        store: 状态库
        only_keys: 仅处理这些 item_key

    Returns:
        退出码（0 全部成功）
    """
    if not only_keys:
        raise ValueError("--only is required")
    local = ZoteroLocal(cfg.zotero.local_api)
    mcp = McpClient(cfg.zotero.mcp_url)
    attacher = Attacher(mcp, local, cfg.paths.archive_dir, archive_copy=False,
                        library_id=cfg.zotero.library_id,
                        attach_settle_seconds=cfg.fetch.attach_settle_seconds)

    # ---------- 步骤 1: 捞出"已下载未挂载"的记录 ----------
    # 目的: 实现断点续跑；已 attached 的记录不在其中
    rows = store.pending_rows((Stage.DOWNLOADED,))
    if only_keys:
        rows = [r for r in rows if r["item_key"] in only_keys]

    if not rows:
        print("没有待挂载的条目（先运行 fetch）。")
        local.close()
        return 0

    print(f"待挂载：{len(rows)} 篇")
    ok_count = 0
    fail_count = 0

    for i, row in enumerate(rows, 1):
        item_key = row["item_key"]
        file_path = Path(row["file_path"] or "")
        title = row["title"] or item_key
        print("-" * 68)
        print(f"[{i}/{len(rows)}] {title[:80]}")

        # ---------- 步骤 2: 确认文件仍在 ----------
        # 目的: 暂存目录可能被清理；文件缺失时明确报错而不是静默失败
        if not file_path.exists():
            LOG.error("文件不存在：%s", file_path)
            store.mark(item_key, Stage.FAILED, note=f"文件不存在：{file_path}")
            fail_count += 1
            continue

        validation = check_pdf(file_path, expected_title=row["title"] or "", expected_doi=row["doi"] or "",
                               min_bytes=cfg.fetch.min_pdf_bytes,
                               similarity_threshold=cfg.fetch.title_similarity,
                               expected_authors=json.loads(row.get('authors') or '[]'),
                               institutional_authors=row.get('institutional_authors') == '1',
                               inbox_matched=row.get('source') == 'inbox' and row.get('inbox_matched') == '1',
                               max_bytes=cfg.fetch.max_pdf_bytes,
                               allow_scanned=row.get('source') == 'inbox' and row.get('inbox_matched') == '1')
        if not validation.ok:
            try:
                rejected = quarantine_pdf(file_path, cfg.paths.staging_dir, item_key, max_bytes=cfg.fetch.max_pdf_bytes)
            except (OSError, ValueError):
                rejected = file_path
            store.mark(item_key, Stage.FAILED, file_path=str(rejected),
                       note=f"校验失败：{validation.reason}", bump_attempts=True)
            fail_count += 1
            continue

        if '人工核对元数据' in getattr(validation, 'reason', ''):
            LOG.warning('[%s] %s', item_key, validation.reason)

        # ---------- 步骤 3: 构造 PaperNeed 并挂载 ----------
        paper = PaperNeed(
            item_key=item_key,
            title=row["title"] or "",
            doi=row["doi"] or "",
            year=_year_from_title_row(row),
            collection_key=cfg.zotero.collection_key,
        )
        result: AttachResult = attacher.attach(paper, file_path, title=file_path.name)

        if result.ok and result.verified:
            store.mark(
                item_key,
                Stage.ATTACHED,
                attachment_key=result.attachment_key,
                note=result.detail,
            )
            if result.attachment_key:
                print(f"  ✅ 已挂载（附件 key 仅供参考：{result.attachment_key}）")
            else:
                print(f"  ✅ 已挂载：{result.detail}")
            ok_count += 1
        else:
            store.mark(item_key, Stage.FAILED, attachment_key='', note=result.detail, bump_attempts=True)
            print(f"  ❌ 挂载失败：{result.detail}")
            fail_count += 1

    print("=" * 68)
    print(f"挂载完成：成功 {ok_count}，失败 {fail_count}")
    local.close()
    return 1 if fail_count else 0


def _year_from_title_row(row: dict) -> str:
    """从状态库记录中推断年份（attach 阶段构造 PaperNeed 时用）。

    在全局中的作用:
        attach 只需极少字段；年份用于命名兜底，缺失不影响挂载。

    Args:
        row: 状态库记录

    Returns:
        4 位年份字符串或空串
    """
    import re

    match = re.search(r"(19|20)\d{2}", row.get("title", "") or "")
    return match.group(0) if match else ""


# ======================================================================
# 子命令：report（出报告）
# ======================================================================

def _md(value) -> str:
    text = ' '.join(str(value or '').split())
    # HTML entities prevent Markdown image/link/table and raw HTML interpretation.
    return ''.join(f'&#{ord(c)};' if c in r'\`*_{}[]()<>!|#~:/' else html.escape(c, quote=True) for c in text)


def cmd_report(cfg: Config, store: Store) -> int:
    """生成 Markdown 报告。

    在全局中的作用:
        给用户一份可读的最终结果（哪些成功、哪些失败、失败原因）。

    实现效果:
        写入 data/reports/YYYY-MM-DD-HHMM-报告.md 并打印路径。

    Args:
        cfg: 配置
        store: 状态库

    Returns:
        退出码
    """
    from datetime import datetime

    rows = store.all_rows()
    counts = store.counts_by_stage()
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    out_name = f"{stamp}-报告.md"

    lines: list[str] = []
    lines.append(f"# Zotero 文献流水线报告 · {stamp}")
    lines.append("")
    lines.append(f"- 集合 key：{_md(cfg.zotero.collection_key)}")
    lines.append(f"- 归档目录：{_md(str(cfg.paths.archive_dir))}")
    lines.append("")

    # ---------- 统计汇总 ----------
    lines.append("## 一、状态汇总")
    lines.append("")
    lines.append("| 状态 | 数量 |")
    lines.append("|---|---|")
    for stage in Stage:
        lines.append(f"| {stage.value} | {counts.get(stage.value, 0)} |")
    lines.append("")

    # ---------- 明细表 ----------
    lines.append("## 二、明细")
    lines.append("")
    lines.append("| Zotero条目 | 标题 | 来源 | 许可证 | 开放状态 | 状态 | 备注 |")
    lines.append("|---|---|---|---|---|---|---|")
    for row in rows:
        key = row.get('item_key', '')
        safe_key(key)
        fields = [_md((row.get('title') or '')[:70]), _md(row.get('source') or '-'),
                  _md(row.get('license') or '-'), _md(row.get('oa_status') or '-'),
                  _md(row.get('stage') or ''), _md(row.get('note') or '')]
        lines.append(f"| [{key}](zotero://select/library/items/{key}) | " + ' | '.join(fields) + ' |')
    lines.append("")

    # ---------- 待处理清单 ----------
    failed = [r for r in rows if r.get("stage") == Stage.FAILED.value]
    if failed:
        lines.append("## 三、需要人工处理")
        lines.append("")
        for r in failed:
            lines.append(f"- **{_md(r.get('title','')[:60])}**（{_md(r.get('item_key'))}）：{_md(r.get('note') or '无说明')}")
        lines.append("")

    out = write_bytes(cfg.paths.report_dir, out_name, "\n".join(lines).encode("utf-8"), max_bytes=8*1024*1024)
    print(f"报告已生成：{out}")
    print()
    print("状态汇总：", counts)
    return 0


# ======================================================================
# 子命令：run（一键全流程）
# ======================================================================

def cmd_run(cfg: Config, store: Store, only_keys: list[str], free_only: bool = False,
            plugin_names: list[str] | None = None) -> int:
    if not only_keys:
        raise ValueError("--only is required")
    if cmd_scan(cfg, store):
        return 1
    fetched = cmd_fetch(cfg, store, only_keys, free_only, plugin_names)
    if fetched == 2:
        return 2
    attached = cmd_attach(cfg, store, only_keys)
    cmd_report(cfg, store)
    return int(bool(fetched or attached))

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zotero-pdf-pipeline", description="Zotero PDF pipeline")
    parser.add_argument("--config", type=Path, help="配置路径，默认当前目录 config.toml")
    parser.add_argument("--collection", help="Zotero 集合 key")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "scan", "report"):
        sub.add_parser(name)
    for name in ("fetch", "attach", "run"):
        command = sub.add_parser(name)
        command.add_argument("--only", nargs="+", required=True, metavar="KEY")
        if name != "attach":
            modes = command.add_mutually_exclusive_group()
            modes.add_argument("--free-only", action="store_true", help="仅内置来源")
            modes.add_argument("--plugin", action="append", default=[], metavar="NAME", help="启用配置中允许的插件；可重复")
    return parser

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError, ValueError, TypeError) as exc:
        print(f"配置错误：{type(exc).__name__}", file=sys.stderr)
        return 2
    if args.collection:
        cfg.zotero.collection_key = args.collection
    if args.command != "report" and (not cfg.zotero.collection_key or cfg.zotero.collection_key == "YOUR_COLLECTION_KEY"):
        print("请使用 --collection 或设置 [zotero].collection_key", file=sys.stderr)
        return 2
    setup_logging(args.verbose)
    lock = RunLock(cfg.paths.state_db.parent)
    try:
        lock.__enter__()
    except (RunLockedError, OSError, ValueError) as exc:
        LOG.error('无法获得运行锁（%s）', type(exc).__name__)
        return 2
    store = None
    try:
        store = Store(cfg.paths.state_db)
        if args.command == "doctor": return cmd_doctor(cfg)
        if args.command == "scan": return cmd_scan(cfg, store)
        if args.command == "report": return cmd_report(cfg, store)
        if args.command == "fetch": return cmd_fetch(cfg, store, args.only, args.free_only, args.plugin)
        if args.command == "attach": return cmd_attach(cfg, store, args.only)
        if args.command == "run": return cmd_run(cfg, store, args.only, args.free_only, args.plugin)
        return 2
    finally:
        if store is not None: store.close()
        lock.__exit__(None, None, None)

if __name__ == "__main__":
    raise SystemExit(main())
