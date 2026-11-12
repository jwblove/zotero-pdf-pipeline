"""
文件名: test_core.py
功能: 对无外部依赖的核心逻辑做单元测试（命名、校验、状态库、模型）
全局作用:
    本文件位于测试层；只覆盖"纯逻辑"，不触碰网络与 Zotero，
    因此可以在任何环境秒级跑完，作为重构的安全网。
实现效果:
    python3 -m pytest tests/ -q  即可验证最易出错的三处逻辑：
    文件名长度/合法性、标题相似度判定、状态机幂等性。
依赖: pytest
"""

# ==================== 依赖导入 ====================
# 标准库：路径、临时目录
# 第三方库：pytest
# 本地模块：被测对象
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

# 把 src 加入导入路径（使测试可直接 import 模块）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from zotero_pdf_pipeline.models import PaperNeed, Stage, normalize_doi  # noqa: E402
from zotero_pdf_pipeline.naming import build_filename, format_authors, sanitize_filename, unique_path  # noqa: E402
from zotero_pdf_pipeline.state import Store  # noqa: E402
from zotero_pdf_pipeline.validate import is_pdf_magic, normalize_title, title_similarity  # noqa: E402


# ======================================================================
# 一、模型层
# ======================================================================

class TestModels:
    """PaperNeed 与 DOI 规范化。"""

    def test_normalize_doi_strips_prefixes(self):
        """DOI 前缀与尾部标点应被剥离。"""
        assert normalize_doi("https://doi.org/10.1002/ADMA.202306061.") == "10.1002/adma.202306061"
        assert normalize_doi("doi:10.1021/acsnano.5c12182") == "10.1021/acsnano.5c12182"
        assert normalize_doi("  10.1038/s41586-024-07625-4  ") == "10.1038/s41586-024-07625-4"

    def test_paper_need_strips_html_from_title(self):
        """Zotero 抓来的标题含 HTML 标签时应被清理。"""
        paper = PaperNeed(item_key="X", title="Hybrid Chiral MoS<sub>2</sub> Layers")
        assert paper.title == "Hybrid Chiral MoS2 Layers"

    def test_non_article_is_filtered(self):
        """误入库的非文献条目（如 GitHub 仓库）应被识别为非文献。"""
        repo = PaperNeed(item_key="X", title="imjustprism/Void", item_type="computerProgram")
        assert not repo.is_research_article

        article = PaperNeed(item_key="Y", title="A paper", item_type="journalArticle")
        assert article.is_research_article


# ======================================================================
# 二、命名层
# ======================================================================

class TestNaming:
    """文件命名：合法性、长度、作者格式。"""

    def test_format_authors(self):
        """作者前缀应遵循 Zotero 的中文习惯（等/和）。"""
        assert format_authors(["Wan"]) == "Wan"
        assert format_authors(["Wan", "Qiu"]) == "Wan和Qiu"
        assert format_authors(["Wan", "Qiu", "Ren", "Zhou"]) == "Wan等"
        assert format_authors([]) == ""

    def test_sanitize_removes_illegal_chars(self):
        """Windows 非法字符应被替换，且不得以点或空格结尾。"""
        result = sanitize_filename('MoS2/NbSe2: "a study?" ')
        assert "/" not in result and ":" not in result and '"' not in result
        assert not result.endswith(" ") and not result.endswith(".")

    def test_build_filename_matches_zotero_style(self):
        """文件名应与 Zotero 规范一致：作者 - 年 - 标题.pdf"""
        name = build_filename(["Bian"], "2023", "Chiral Van Der Waals Superlattices")
        assert name == "Bian - 2023 - Chiral Van Der Waals Superlattices.pdf"

    def test_build_filename_respects_byte_limit(self):
        """超长中文标题也必须控制在字节上限内以减少路径和重名问题。"""
        long_title = "超长标题测试" * 60
        name = build_filename(["张三", "李四", "王五"], "2026", long_title)
        assert len(name.encode("utf-8")) <= 230, f"文件名过长：{len(name.encode('utf-8'))} 字节"
        assert name.endswith(".pdf")

    def test_build_filename_handles_no_year(self):
        """年份缺失时仍应产生可读文件名。"""
        name = build_filename(["Smith"], "", "Some Title")
        assert name == "Smith - Some Title.pdf"

    def test_unique_path_avoids_collision(self):
        """同名文件应自动追加序号，而不是覆盖。"""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "a.pdf").write_bytes(b"%PDF-1.4")
            candidate = unique_path(d, "a.pdf")
            assert candidate.name == "a 1.pdf"


# ======================================================================
# 三、校验层
# ======================================================================

class TestValidate:
    """PDF 魔数、标题规范化与相似度。"""

    def test_is_pdf_magic(self):
        """应正确识别 PDF 魔数。"""
        with tempfile.TemporaryDirectory() as tmp:
            ok = Path(tmp) / "ok.pdf"
            ok.write_bytes(b"%PDF-1.7\n...")
            bad = Path(tmp) / "bad.pdf"
            bad.write_bytes(b"<html>error</html>")
            assert is_pdf_magic(ok)
            assert not is_pdf_magic(bad)

    def test_normalize_title_unifies_punctuation(self):
        """标点与连字符差异不应影响比对。"""
        assert normalize_title("MoS2‐Based, Chiral!") == "mos2basedchiral"

    def test_normalize_title_handles_subscript_linebreak(self):
        """PDF 把下标拆到下一行时，仍应与标题匹配。

        背景：真实 PDF 首页提取出的文本是 "MoS\\n2"，
        若不删空格就变成 "mos 2"，与标题的 "mos2" 对不上，
        会把完全正确的文件误判为失败。
        """
        assert normalize_title("MoS\n2 Detectors") == "mos2detectors"
        assert normalize_title("MoS 2 Detectors") == "mos2detectors"

    def test_similarity_high_for_containment(self):
        """PDF 首页文本包含标题时应得高分（首页通常还有作者行）。"""
        expected = "Chiral molecular intercalation superlattices"
        actual = "Chiral molecular intercalation superlattices Qi Qian Huaying Ren"
        assert title_similarity(expected, actual) > 0.8

    def test_similarity_high_for_real_acs_layout(self):
        """真实 ACS 排版（标题拆行 + 下标拆行 + 作者行）应判定为匹配。

        这段文本取自实际下载的 PDF，是回归测试：
        修复前相似度只有 0.04，会把正确文件判为失败。
        """
        expected = (
            "Ultrasensitive Circularly Polarized Photon Detectors Based on "
            "Chiral Two-Dimensional MoS2"
        )
        actual = (
            "Ultrasensitive Circularly Polarized Photon\n"
            "Detectors Based on Chiral Two-Dimensional\n"
            "MoS\n2\nYe Wang,* Yiru Zhu, Tieyuan Bian, Feng Ding, Jun Yin"
        )
        assert title_similarity(expected, actual) > 0.80

    def test_similarity_low_for_different_paper(self):
        """完全不同的论文应得低分（防止挂错文献）。"""
        a = "Chiral molecular intercalation superlattices"
        b = "Skyrmionic transport and first-order phase transitions in chiral magnets"
        assert title_similarity(a, b) < 0.55


# ======================================================================
# 四、状态层
# ======================================================================

class TestState:
    """状态库的幂等性（防止重复烧积分）。"""

    def test_deferred_is_retried_but_not_terminal(self):
        """deferred 应仍可重试（只是排到队尾），而不是终态。

        引入 deferred 后应满足：
            ① needs_work 仍为 True（以后还要重试）
            ② 但它排在普通条目之后（由 cli 的排序逻辑保证）
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            store.upsert_need(PaperNeed(item_key="K1", title="T"))
            store.mark("K1", Stage.DEFERRED, note="机器人未找到文献")

            assert store.get_stage("K1") == Stage.DEFERRED.value
            assert store.needs_work("K1"), "deferred 仍需重试，不应视为终态"
            store.close()

    def test_upsert_and_stage(self):
        """登记后阶段应为 pending，且可被推进。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            paper = PaperNeed(item_key="K1", title="T", doi="10.1/x")
            store.upsert_need(paper)
            assert store.get_stage("K1") == Stage.PENDING.value

            store.mark("K1", Stage.DOWNLOADED, file_path="/tmp/a.pdf")
            assert store.get_stage("K1") == Stage.DOWNLOADED.value
            assert store.get("K1")["file_path"] == "/tmp/a.pdf"
            store.close()

    def test_upsert_does_not_reset_stage(self):
        """重复 upsert 不得把已完成条目打回 pending（幂等关键）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            paper = PaperNeed(item_key="K1", title="T")
            store.upsert_need(paper)
            store.mark("K1", Stage.ATTACHED, attachment_key="A1")

            # 再次扫描同一集合会重复 upsert
            store.upsert_need(paper)
            assert store.get_stage("K1") == Stage.ATTACHED.value
            assert not store.needs_work("K1"), "已挂载条目不应再被处理"
            store.close()

    def test_partial_mark_keeps_previous_values(self):
        """部分更新不应清空已有字段。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            store.upsert_need(PaperNeed(item_key="K1", title="T"))
            store.mark("K1", Stage.DOWNLOADED, file_path="/tmp/a.pdf", source="openalex")
            store.mark("K1", Stage.ATTACHED, attachment_key="A1")

            row = store.get("K1")
            assert row["file_path"] == "/tmp/a.pdf"   # 保持
            assert row["source"] == "openalex"        # 保持
            assert row["attachment_key"] == "A1"      # 更新
            store.close()

    def test_failed_retries_until_budget_exhausted(self):
        """failed 应在重试预算内可重试，超出预算后变成终态。

        设计意图:
            暂时性失败（网络抖动、站点限流）值得重试；
            但重试必须有上限，否则每次运行都会重新发布求助、持续烧积分。
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            store.upsert_need(PaperNeed(item_key="K1", title="T"))

            # 第一次失败：attempts=1，仍在预算（默认 3）内，允许重试
            store.mark("K1", Stage.FAILED, note="第一次失败", bump_attempts=True)
            assert store.needs_work("K1"), "首次失败应允许重试"

            # 累加到 3 次：预算耗尽，变为终态
            store.mark("K1", Stage.FAILED, note="第二次", bump_attempts=True)
            store.mark("K1", Stage.FAILED, note="第三次", bump_attempts=True)
            assert store.attempts_of("K1") == 3
            assert not store.needs_work("K1"), "超出重试预算后不应再处理"
            store.close()
