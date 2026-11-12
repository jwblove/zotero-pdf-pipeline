"""
文件名: naming.py
功能: 按 Zotero 桌面端命名规范生成 PDF 文件名，并规避 Windows 长度限制
全局作用:
    本文件位于命名工具层，被 sources（决定下载落地名）与 attach 流程使用。
    上游：PaperNeed（标题/作者/年）
    下游：归档目录中的实际文件名，以及挂载时的附件标题
实现效果:
    产出的文件名与 Zotero "文件重命名" 功能一致：
        "作者 等 - 年 - 标题.pdf" / "作者A和作者B - 年 - 标题.pdf" / "作者 - 年 - 标题.pdf"
    同时保证：文件名合法（去非法字符）、总字节 < 230（留出 Windows 255 上限余量），
    且不同论文之间的文件名稳定可预测。
依赖: 仅标准库
"""

# ==================== 依赖导入 ====================
# 标准库：正则、路径、Unicode 规范化
# 说明: 本模块零第三方依赖，便于被任意模块与测试直接引用
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# ==================== 模块级常量 ====================

# Windows 非法文件名字符
ILLEGAL_CHARS = r'[<>:"/\\|?*\x00-\x1f]'

# Windows 文件名总字节上限附近的安全阈值。
# 说明: Windows 通常限制单文件名的 UTF-16 单元数；保守采用 230 UTF-8 字节，给 ".pdf"、
#       以及可能追加的空格和序号留出余量。
#       注意：中文标题按 UTF-8 计，一个字 3 字节，因此这里必须按"字节"而非"字符"截断。
MAX_FILENAME_BYTES = 230

# 标题截断长度上限（字符数），与 Zotero 观察到的 ~120 字符行为接近但更宽松
MAX_TITLE_CHARS = 110


def sanitize_filename(text: str, replacement: str = "-") -> str:
    """把任意字符串清洗为合法的文件名单段。

    在全局中的作用:
        build_filename 的底层工具；也被 sources 用于把 DOI 转成安全文件名。

    实现效果:
        非法字符替换为 replacement；连续空白压成单空格；去掉首尾空格与点。

    Args:
        text: 原始字符串（可能含 / \\ : * ? " < > | 与换行）
        replacement: 非法字符的替代符号，默认 "-"

    Returns:
        清洗后的字符串（可能为空串）

    Example:
        >>> sanitize_filename('MoS2/NbSe2: a study?')
        'MoS2-NbSe2- a study-'
    """
    if not text:
        return ""
    # 非法字符统一替换
    cleaned = re.sub(ILLEGAL_CHARS, replacement, text)
    # 连续空白（含制表/换行）压缩为单个空格
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # 去掉结尾的点和空格（Windows 不允许文件名以点或空格结尾）
    cleaned = cleaned.rstrip(" .")
    base = cleaned.split('.')[0].rstrip(' ')
    if re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]|CONIN\$|CONOUT\$|CLOCK\$)', base, re.I):
        cleaned = '_' + cleaned
    return cleaned


def _smart_truncate(text: str, max_bytes: int) -> str:
    """按字节上限安全截断字符串，且不切碎多字节字符。

    在全局中的作用:
        build_filename 的长度保护；避免中文标题把总路径推过 Windows 上限
        以降低同步目录中的文件名冲突。

    实现效果:
        返回不超过 max_bytes 个 UTF-8 字节的字符串；必要时在词边界处截断并加省略号。

    Args:
        text: 待截断文本
        max_bytes: 允许的最大字节数

    Returns:
        截断后的文本
    """
    encoded = text.encode("utf-8")
    if max_bytes <= 3:
        return encoded[:max(0, max_bytes)].decode('utf-8', 'ignore')
    if len(encoded) <= max_bytes:
        return text

    # 预留 3 字节给省略号 "…"
    budget = max_bytes - 3
    truncated = encoded[:budget]
    # 逐字节回退，直到能无损解码（避免把多字节字符切成乱码）
    while truncated:
        try:
            decoded = truncated.decode("utf-8")
            break
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    else:
        return ""

    # 尽量在空格/连字符处断开，看起来更自然
    for sep in (" ", "-", "–", "—", ","):
        idx = decoded.rfind(sep)
        if idx > len(decoded) * 0.5:  # 只在后半段断开，避免截得过短
            decoded = decoded[:idx]
            break
    return decoded.rstrip(" -–—,;:.") + "…"


def format_authors(authors: list[str], max_authors: int = 3) -> str:
    """把作者姓列表格式化成 Zotero 风格的前缀。

    在全局中的作用:
        build_filename 的组成部分，决定文件名开头 "谁 等 - 年"。

    实现效果:
        1 位作者 → "Smith"
        2 位作者 → "Smith和Wang"
        3 位作者 → "Smith等"（Zotero 中文界面用"等"）
        0 位作者 → ""（由调用方兜底为 "Unknown"）

    Args:
        authors: 作者姓氏列表（已按顺序）
        max_authors: 超过几位就用"等"概括，默认 3（Zotero 行为）

    Returns:
        作者前缀字符串

    Example:
        >>> format_authors(["Wan", "Qiu", "Ren", "Zhou"])
        'Wan等'
    """
    cleaned = [a.strip() for a in authors if a and a.strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]}和{cleaned[1]}"
    return f"{cleaned[0]}等"


def build_filename(
    authors: list[str],
    year: str,
    title: str,
    suffix: str = ".pdf",
) -> str:
    """构造 Zotero 风格的 PDF 文件名。

    在全局中的作用:
        下载、归档落盘、挂载附件标题三处共用同一命名逻辑，
        使磁盘文件名与附件标题一致，降低重名风险。

    实现效果:
        形如 "Wan等 - 2024 - Unconventional superconductivity in chiral….pdf"
        总字节数不超过 MAX_FILENAME_BYTES。

    Args:
        authors: 作者姓氏列表
        year: 年份字符串（可为空）
        title: 论文标题（可含 HTML 残留，会被清洗）
        suffix: 扩展名，默认 ".pdf"

    Returns:
        合法且长度受控的文件名

    Example:
        >>> build_filename(["Bian"], "2023", "Chiral Van Der Waals Superlattices", ".pdf")
        'Bian - 2023 - Chiral Van Der Waals Superlattices.pdf'
    """
    # ---------- 步骤 1: 组装作者段 ----------
    # 目的: 得到 "Smith等" 这样的前缀；无作者时降级为 Unknown
    if not re.fullmatch(r'\.[A-Za-z0-9]{1,10}', suffix):
        raise ValueError('invalid suffix')
    author_part = sanitize_filename(format_authors(authors)) or "Unknown"

    # ---------- 步骤 2: 组装年份段 ----------
    # 目的: 只保留 4 位年份，容忍 "11/2023"、"2023-09-11" 之类输入
    year_match = re.search(r"(19|20)\d{2}", year or "")
    year_part = year_match.group(0) if year_match else ""

    # ---------- 步骤 3: 清洗标题 ----------
    # 目的: Unicode 规范化 + 非法字符替换，统一标题形态便于后续比对
    safe_title = unicodedata.normalize("NFKC", title or "")
    safe_title = re.sub('[‐–—]', '-', safe_title)
    safe_title = sanitize_filename(safe_title)
    # 折叠多余标点产生的重复分隔符，例如 "a - - b" -> "a - b"
    safe_title = re.sub(r"(\s*-\s*){2,}", " - ", safe_title).strip(" -")

    # ---------- 步骤 4: 按前缀长度预算截断标题 ----------
    # 目的: 保证 "作者 - 年 - 标题.pdf" 总字节不超上限
    prefix = f"{author_part} - {year_part} - " if year_part else f"{author_part} - "
    prefix_bytes = len(prefix.encode("utf-8"))
    suffix_bytes = len(suffix.encode("utf-8"))
    budget = MAX_FILENAME_BYTES - prefix_bytes - suffix_bytes

    if budget <= 0:
        # 极端情况：作者名本身就超长，退化为只用作者+年份
        fallback = f"{author_part} - {year_part}" if year_part else author_part
        return sanitize_filename(_smart_truncate(fallback, MAX_FILENAME_BYTES - suffix_bytes).strip()) + suffix

    # 标题额外限制为 MAX_TITLE_CHARS 字符，避免出现超长但字节很少的标题
    if len(safe_title) > MAX_TITLE_CHARS:
        safe_title = safe_title[:MAX_TITLE_CHARS].rstrip(" -")
    title_part = _smart_truncate(safe_title, budget)

    return _smart_truncate(sanitize_filename(f"{prefix}{title_part}"), MAX_FILENAME_BYTES-suffix_bytes) + suffix


def unique_path(directory: Path, filename: str) -> Path:
    """在目录中为文件名找一个不冲突的落点。

    在全局中的作用:
        下载落盘前的最后一道保护；与 常见文件名去重行为保持一致
        （追加 " 1"、" 2"），但由我们主动决定，而不是让插件事后追加。

    实现效果:
        返回 directory/filename；若已存在，则依次尝试 "name 1.pdf"、"name 2.pdf" …

    Args:
        directory: 目标目录
        filename: 期望文件名

    Returns:
        可安全写入的完整路径
    """
    from .safe_files import target
    candidate = target(directory, filename)
    if not candidate.exists():
        return candidate

    stem, ext = candidate.stem, candidate.suffix
    for i in range(1, 100):
        addition = f" {i}{ext}"
        alt = target(directory, _smart_truncate(stem, MAX_FILENAME_BYTES-len(addition.encode())) + addition)
        if not alt.exists():
            return alt
    # 极端兜底：加时间戳，保证一定能写
    import time  # 局部导入：仅此兜底分支需要

    addition = f" {int(time.time())}{ext}"
    return target(directory, _smart_truncate(stem, MAX_FILENAME_BYTES-len(addition.encode())) + addition)
