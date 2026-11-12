"""
文件名: models.py
功能: 定义全流水线共享的数据结构（论文需求 / 解析结果 / 下载结果 / 挂载结果）
全局作用:
    本文件位于数据模型层，是整个流水线的"公共语言"。
    上游：zotero_local（产出 PaperNeed）、sources/*（产出 ResolvedFile）
    下游：state（持久化）、report（渲染表格）、cli（编排）
    所有模块之间只通过这些 dataclass 传数据，避免 dict 满天飞导致的字段拼写错误。
实现效果:
    任何一处 new 出 PaperNeed 后，全流程字段名与类型都被静态检查约束。
依赖: 仅标准库（dataclasses / enum / datetime）
"""

# ==================== 依赖导入 ====================
# 标准库：数据类、枚举、时间与类型标注
# 说明: 本模块刻意不引入任何第三方库，保证模型层零依赖、可被任意模块安全导入
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


class Stage(str, Enum):
    """处理阶段枚举。

    在全局中的作用:
        定义一篇论文在流水线中的状态机位置，由 state.Store 持久化到 SQLite。
        终态（attached / failed / skipped）的条目在重跑时会被跳过，这是
        避免重复处理。

    Attributes:
        PENDING: 刚从 Zotero 读取到、尚无任何动作
        RESOLVED: 已找到候选地址
        DOWNLOADED: PDF 已落盘并通过校验
        ATTACHED: 已挂载到 Zotero 条目且回读验证成功（成功终态）
        FAILED: 重试耗尽仍失败（失败终态，保留原因供人工介入）
        SKIPPED: 主动跳过（如非文献类型、已有附件）
            应排到队列末尾，不阻塞其他文献；下轮仍会重试
    """

    PENDING = "pending"
    RESOLVED = "resolved"
    DOWNLOADED = "downloaded"
    ATTACHED = "attached"
    FAILED = "failed"
    SKIPPED = "skipped"

    # 应排到队列最后，而不是阻塞后续能完成的文献。
    # 与 FAILED 的区别：DEFERRED 不算失败，依然会在本轮/下轮被重试。
    DEFERRED = "deferred"


class Source(str, Enum):
    """下载来源枚举。

    在全局中的作用:
        记录 PDF 究竟从哪个渠道得到，写入报告与状态库，
        便于事后审计。
    """

    OPENALEX = "openalex"
    UNPAYWALL = "unpaywall"
    ARXIV = "arxiv"
    INBOX = "inbox"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    EUROPE_PMC = "europe_pmc"
    CORE = "core"
    MANUAL = "manual"  # 人工放进归档目录、由流水线扫描收编


@dataclass
class PaperNeed:
    """一篇"需要 PDF"的论文。

    在全局中的作用:
        scan 阶段的输出、fetch/attach 阶段的输入。
        一份 PaperNeed 对应 Zotero 里一个缺附件的文献条目。

    实现效果:
        携带足够信息让后续模块：① 去网上找 PDF；② 校验 PDF 是否对得上；
        ③ 知道该挂到哪个 Zotero 条目。

    Attributes:
        item_key: Zotero 条目 key（挂载时的目标锚点）
        title: 标题（用于校验下载到的 PDF 是否正确）
        doi: DOI，可能为空字符串（无 DOI 的条目需人工处理）
        year: 年份字符串，可能为空
        item_type: Zotero 条目类型（journalArticle / preprint / computerProgram ...）
        publication: 期刊或来源名称，仅用于报告展示
        collection_key: 所属集合 key，用于报告分组
        has_institutional_authors: 存在机构/单字段作者，跳过负向作者姓名校验
    """

    item_key: str
    title: str
    doi: str = ""
    year: str = ""
    item_type: str = "journalArticle"
    publication: str = ""
    collection_key: str = ""
    authors: list[str] = field(default_factory=list)
    pmcid: str = ""
    has_institutional_authors: bool = False

    def __post_init__(self) -> None:
        """构造后立即清洗字段，避免脏数据渗透到下游。

        目的: Zotero 标题里常含 HTML 片段（如 <sub>2</sub>）和 Unicode 连字符，
              这些会污染文件命名与相似度计算，必须在这里统一处理。
        全局作用: 所有 PaperNeed 实例创建即已规范化，下游可放心使用。
        实现效果: title 去标签、去多余空白；doi 去前缀并小写。
        """
        # 去掉 Zotero 抓取时残留的 HTML 标签，如 MoS<sub>2</sub> -> MoS2
        from .safe_files import safe_key
        safe_key(self.item_key)
        self.title = _strip_html(self.title).strip()
        # 把连续空白压缩成单个空格，统一标题形态
        self.title = " ".join(self.title.split())
        # DOI 规范化：去 https://doi.org/ 前缀、去 doi: 前缀、去首尾空白
        self.doi = normalize_doi(self.doi)

    @property
    def is_research_article(self) -> bool:
        """判断该条目是否为"需要 PDF 的科研文献"。

        在全局中的作用:
            被 scan 用于过滤掉误入库的非文献条目
            （例如误入的非文献条目）。

        Returns:
            True 表示是需要下载 PDF 的文献类型
        """
        # 只有这些类型才真正对应"一篇论文/预印本/学位论文"
        research_types = {
            "journalArticle",
            "preprint",
            "conferencePaper",
            "thesis",
            "report",
            "bookSection",
        }
        return self.item_type in research_types


@dataclass
class ResolvedFile:
    """一次成功的 PDF 解析结果。

    在全局中的作用:
        sources/* 各源 resolve() 的返回值，也是 fetch 阶段传给 validate 与 state 的对象。

    Attributes:
        paper: 对应的论文需求（回链，便于记账）
        file_path: 下载后 PDF 的本地路径
        source: 来源枚举
        url: 实际下载 URL（审计用）
        bytes_size: 文件字节数
        note: 附加说明
    """

    paper: PaperNeed
    file_path: Path
    source: Source
    url: str = ""
    bytes_size: int = 0
    note: str = ""
    license: str = ""
    oa_status: str = ""
    inbox_matched: bool = False


@dataclass
class AttachResult:
    """一次挂载尝试的结果。

    在全局中的作用:
        attach 阶段的输出，决定 state 里该条记录是落 ATTACHED 还是 FAILED。

    Attributes:
        item_key: 目标 Zotero 条目
        ok: 是否成功（含回读验证）
        attachment_key: 回读时第一个 PDF 的 key，仅供参考，之后可能被附加组件改变
        detail: 人类可读的说明或错误原因
        verified: 是否通过"回读 Zotero 确认附件存在"的校验
    """

    item_key: str
    ok: bool
    attachment_key: str = ""
    detail: str = ""
    verified: bool = False


@dataclass
class RunStats:
    """一次运行的统计汇总（用于报告尾部）。

    在全局中的作用:
        由 cli 在编排过程中累计，最终交给 report.render_markdown 输出。

    Attributes:
        scanned: 扫描到的缺附件条目数
        downloaded: 成功下载数
        attached: 成功挂载数
        failed: 失败数
        skipped: 跳过数
        started_at / finished_at: 运行起止时间
        per_source: 各来源贡献计数
    """

    scanned: int = 0
    downloaded: int = 0
    attached: int = 0
    failed: int = 0
    skipped: int = 0
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None
    per_source: dict[str, int] = field(default_factory=dict)


# ==================== 模块级工具函数 ====================

def _strip_html(text: str) -> str:
    """剥离字符串中的 HTML 标签。

    在全局中的作用:
        PaperNeed.__post_init__ 的辅助函数；Zotero 抓取 ACS/Wiley 时
        标题常带 <sub>/<i> 标签，若不清理会污染文件名和相似度比对。

    Args:
        text: 可能含 HTML 标签的原始字符串

    Returns:
        去掉 <...> 片段后的纯文本

    Example:
        >>> _strip_html("Hybrid Chiral MoS<sub>2</sub> Layers")
        'Hybrid Chiral MoS2 Layers'
    """
    import re  # 局部导入：本函数是唯一需要正则的地方，避免污染模块命名空间

    # 删除所有形如 <sub> </sub> <i/> 的标签片段
    return re.sub(r"<[^>]+>", "", text or "")


def normalize_doi(value: str) -> str:
    """把各种 DOI 写法统一成裸 DOI 字符串。

    在全局中的作用:
        被 PaperNeed.__post_init__ 调用，也被 sources/* 用于构造 API 请求；
        统一形态可避免"同一篇论文因 DOI 写法不同被当成两条记录"。

    Args:
        value: 形如 'https://doi.org/10.x/y'、'doi:10.x/y' 或 '10.x/y'

    Returns:
        裸 DOI（小写、无前缀、无尾部标点）；无法识别时返回去空白后的原文

    Example:
        >>> normalize_doi("https://doi.org/10.1002/ADMA.202306061.")
        '10.1002/adma.202306061'
    """
    import re  # 同上：局部导入，保持模块顶部零依赖

    text = (value or "").strip()
    # 去掉 URL 形态前缀（http/https、dx.doi.org）
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    # 去掉 "doi:" 前缀
    text = re.sub(r"^doi\s*:\s*", "", text, flags=re.IGNORECASE)
    # 去掉句末常见标点（从网页复制时常带）
    text = text.rstrip(".,;)]}\u3002\uff09")
    # 只有形如 10.xxxx/yyyy 的才算合法 DOI，统一转小写便于比对
    if re.fullmatch(r"10\.\d{4,9}/\S+", text):
        return text.lower()
    return text
