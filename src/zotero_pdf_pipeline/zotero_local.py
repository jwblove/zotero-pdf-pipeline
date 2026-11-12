"""
文件名: zotero_local.py
功能: 通过 Zotero 桌面版本地 HTTP API 只读读取条目、集合与附件
全局作用:
    本文件位于 Zotero 只读通道，是 scan 阶段的数据来源，也是 attach 之后的校验手段。
    上游：Zotero 桌面端（127.0.0.1:23119）
    下游：cli（编排）、attach（回读校验挂载是否真的成功）
实现效果:
    能够：列出集合内条目、找出缺附件的文献条目、查询某条目的附件列表、
    查询某条目的作者列表（用于文件命名）。全程只读，不修改 Zotero 数据。
依赖: httpx
"""

# ==================== 依赖导入 ====================
# 标准库：类型与日志
# 第三方库：httpx - 用途：带超时与 JSON 解析的 HTTP 客户端
# 本地模块：models - 用途：PaperNeed 数据结构与 DOI 规范化
from __future__ import annotations

import logging
import hashlib
from typing import Any

import httpx

from .models import PaperNeed, normalize_doi
from .http_client import make_client
from .safe_files import safe_key
import json
import time

LOG = logging.getLogger("pipeline.zotero")


class ZoteroLocalError(RuntimeError):
    """本地 API 访问失败异常。

    在全局中的作用:
        当 Zotero 未启动或未开启"允许其他应用通信"时抛出，
        由 cli 捕获后给出可操作的中文提示（而不是让用户看 ConnectionRefused）。
    """


class ZoteroLocal:
    """Zotero 本地只读 API 客户端。

    在全局中的作用:
        scan 阶段的唯一数据入口；attach 阶段的验证器。
        所有方法都是只读 GET，绝不调用写接口。

    Attributes:
        base: API 基址，如 http://127.0.0.1:23119/api/users/0

    Example:
        >>> z = ZoteroLocal("http://127.0.0.1:23119/api/users/0")
        >>> needs = z.items_without_attachment("COLLECTION_KEY")
        >>> len(needs)
        7
    """

    def __init__(self, base: str, timeout: int = 30) -> None:
        """初始化客户端。

        Args:
            base: API 基址（不含尾斜杠）
            timeout: 单次请求超时秒数
        """
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._client = make_client(timeout=timeout)

    # ------------------------------------------------------------------
    # 基础请求
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> Any:
        """内部 GET 封装，统一错误处理与 JSON 解析。

        目的: 把网络异常转换成带中文说明的 ZoteroLocalError。
        全局作用: 所有公开方法都经过此函数，错误处理只需一处。
        实现效果: 返回解析后的 Python 对象（list/dict）。

        Args:
            path: 形如 /collections/XXXX/items 的路径
            params: 查询参数

        Returns:
            解析后的 JSON 数据

        Raises:
            ZoteroLocalError: 连接失败、超时或非 2xx 响应
        """
        url = f"{self.base}{path}"
        deadline = time.monotonic() + self.timeout
        try:
            with self._client.stream('GET', url, params=params or {}, timeout=self.timeout) as resp:
                if resp.status_code != 200:
                    raise ZoteroLocalError(f"Local API HTTP {resp.status_code}")
                maximum = 4 * 1024 * 1024
                length = int(resp.headers.get('Content-Length', '0'))
                if not 0 <= length <= maximum: raise ZoteroLocalError('Local API response size limit')
                body = bytearray()
                for chunk in resp.iter_bytes(chunk_size=65536):
                    if len(body)+len(chunk)>maximum or time.monotonic()>=deadline:
                        raise ZoteroLocalError('Local API size/deadline limit')
                    body.extend(chunk)
            value = json.loads(body)
            if not isinstance(value, (dict,list)): raise ZoteroLocalError('Invalid local API response shape')
            entries = value if isinstance(value,list) else [value]
            for entry in entries:
                if not isinstance(entry,dict) or ('data' in entry and not isinstance(entry['data'],dict)):
                    raise ZoteroLocalError('Invalid local API item shape')
                data = entry.get('data',{})
                key = data.get('key') or entry.get('key')
                if key is not None: safe_key(key)
                creators = data.get('creators') or []
                if not isinstance(creators,list) or any(not isinstance(c,dict) for c in creators):
                    raise ZoteroLocalError('Invalid local API creators')
                for creator in creators:
                    for field in ('lastName','firstName','name','creatorType'):
                        if creator.get(field) is not None and not isinstance(creator[field],str):
                            raise ZoteroLocalError('Invalid local API creator field')
                for field in ('key','title','DOI','date','itemType','publicationTitle','repository'):
                    if data.get(field) is not None and not isinstance(data[field],str):
                        raise ZoteroLocalError('Invalid local API field')
            return value
        except ZoteroLocalError: raise
        except (TimeoutError, httpx.HTTPError, OSError) as exc:
            raise ZoteroLocalError('Local API connection failed or timed out') from None
        except (ValueError, TypeError, RecursionError) as exc:
            raise ZoteroLocalError('Invalid local API JSON/response') from None

    def ping(self) -> bool:
        """探测本地 API 是否可用（doctor 用）。

        Returns:
            True 表示可连通
        """
        try:
            self._get("/items", {"limit": 1})
            return True
        except ZoteroLocalError as exc:
            LOG.debug("ping 失败：%s", exc)
            return False

    # ------------------------------------------------------------------
    # 读取集合与条目
    # ------------------------------------------------------------------

    def collection_name(self, collection_key: str) -> str:
        """读取集合名称（用于报告可读性）。

        Args:
            collection_key: 集合 key

        Returns:
            集合名；失败时返回 key 本身
        """
        try:
            safe_key(collection_key)
            data = self._get(f"/collections/{collection_key}")
            if not isinstance(data,dict): raise ZoteroLocalError("Invalid collection response")
            return data.get("data", {}).get("name", collection_key)
        except ZoteroLocalError:
            return collection_key

    def _paginated_items(self, path: str, page_size: int, response_name: str) -> list[dict]:
        """读取全部分页，限制总数并拒绝重复的完整页面。"""
        results: list[dict] = []
        start = 0
        seen_pages: set[bytes] = set()
        while True:
            batch = self._get(path, {"limit": page_size, "start": start})
            if not isinstance(batch, list) or any(
                not isinstance(row, dict)
                or ("data" in row and not isinstance(row["data"], dict))
                for row in batch
            ):
                raise ZoteroLocalError(f"Invalid {response_name} response")
            if len(results) + len(batch) > 100000:
                raise ZoteroLocalError(f"{response_name} item limit")
            if len(batch) >= page_size:
                fingerprint = hashlib.sha256(
                    json.dumps(batch, sort_keys=True, ensure_ascii=True).encode("ascii")
                ).digest()
                if fingerprint in seen_pages:
                    raise ZoteroLocalError(f"Repeated {response_name} page")
                seen_pages.add(fingerprint)
            results.extend(batch)
            if len(batch) < page_size:
                return results
            start += len(batch)

    def items_in_collection(self, collection_key: str) -> list[dict]:
        """列出集合内全部条目（含子条目，如附件与笔记）。

        在全局中的作用:
            扫描的起点；返回原始条目 dict，由调用方筛选。

        实现效果:
            处理分页：每页请求 100 条，使用 start 移动到下一页。

        Args:
            collection_key: 集合 key

        Returns:
            条目 dict 列表

        Example:
            >>> items = z.items_in_collection("COLLECTION_KEY")
            >>> items[0]["data"]["itemType"]
            'preprint'
        """
        safe_key(collection_key)
        return self._paginated_items(f"/collections/{collection_key}/items", 100, "collection items")

    def attachments_of(self, item_key: str) -> list[dict]:
        """查询某条目的附件子项。

        在全局中的作用:
            判断"是否已有附件"的核心方法；也用于 attach 后的回读校验。

        Args:
            item_key: 父条目 key

        Returns:
            附件条目 dict 列表（仅 itemType == 'attachment'）
        """
        safe_key(item_key)
        children = self._paginated_items(f"/items/{item_key}/children", 25, "children")
        return [c for c in children if c.get("data", {}).get("itemType") == "attachment"]

    def item(self, item_key: str) -> dict:
        """读取单条目详情。

        Args:
            item_key: 条目 key

        Returns:
            条目 dict
        """
        safe_key(item_key)
        value = self._get(f"/items/{item_key}")
        if not isinstance(value,dict): raise ZoteroLocalError("Invalid item response")
        return value

    def creators_of(self, item_key: str) -> list[str]:
        """提取条目作者的姓氏列表（用于文件命名）。

        在全局中的作用:
            为 naming.build_filename 提供作者前缀；
            姓名缺失时退化为机构名或全名，保证不返回空。

        Args:
            item_key: 条目 key

        Returns:
            姓氏字符串列表，保持原顺序

        Example:
            >>> z.creators_of("8LJ9KLLS")
            ['Bian', 'Nakano', 'Miyata']
        """
        data = self.item(item_key).get("data", {})
        surnames: list[str] = []
        for creator in data.get("creators", []) or []:
            # 机构作者只有 name 字段；个人作者用 lastName
            surname = (creator.get("lastName") or creator.get("name") or "").strip()
            if not surname:
                # 极端兜底：用全名首词
                surname = (creator.get("firstName") or "").split(" ")[0].strip()
            if surname:
                surnames.append(surname)
        return surnames

    # ------------------------------------------------------------------
    # 业务层：找出需要 PDF 的条目
    # ------------------------------------------------------------------

    def items_without_attachment(self, collection_key: str) -> list[PaperNeed]:
        """找出集合中"缺附件"的文献条目。

        在全局中的作用:
            scan 子命令的核心逻辑，直接产出后续下载流程的输入清单。

        实现效果:
            过滤掉：非文献类型（如误入库的 computerProgram）、已有附件的条目；
            对每个符合条件的条目构造 PaperNeed（含 DOI/标题/年/期刊）。

        Args:
            collection_key: 集合 key

        Returns:
            PaperNeed 列表

        Example:
            >>> needs = z.items_without_attachment("COLLECTION_KEY")
            >>> [n.doi for n in needs][:2]
            ['10.1021/acsnano.5c12182', '10.1021/acs.nanolett.5c03047']
        """
        out: list[PaperNeed] = []

        # ---------- 步骤 1: 取集合内所有顶层条目 ----------
        # 目的: 得到候选池；附件和笔记会在步骤 2 被过滤掉
        raw_items = self.items_in_collection(collection_key)

        for raw in raw_items:
            data = raw.get("data", {})
            item_type = data.get("itemType", "")

            # ---------- 步骤 2: 跳过附件/笔记等非顶层条目 ----------
            # 目的: 只关心"论文本身"，附件条目本身不需要再找 PDF
            if item_type in ("attachment", "note", "annotation"):
                continue

            item_key = data.get("key") or raw.get("key", "")
            if not item_key:
                continue

            # ---------- 步骤 3: 构造 PaperNeed 并过滤非文献 ----------
            # 目的: 例如 GitHub 仓库条目（computerProgram）不应被当作论文下载
            paper = PaperNeed(
                item_key=item_key,
                title=data.get("title", "") or "",
                doi=normalize_doi(data.get("DOI", "") or ""),
                year=_extract_year(data.get("date", "") or ""),
                item_type=item_type,
                publication=data.get("publicationTitle", "")
                or data.get("repository", "")
                or "",
                collection_key=collection_key,
                authors=[c.get("lastName") or c.get("name", "") for c in data.get("creators", []) if c.get("creatorType") == "author"],
                has_institutional_authors=any(c.get('creatorType') == 'author' and
                    (c.get('fieldMode') == 1 or bool(c.get('name') and not c.get('lastName')))
                    for c in data.get('creators', [])),
                pmcid=_extract_pmcid(data),
            )
            if not paper.is_research_article:
                LOG.info("跳过非文献条目 [%s] type=%s title=%s", item_key, item_type, paper.title[:50])
                continue

            # ---------- 步骤 4: 判断是否已有附件 ----------
            # 目的: 已有 PDF 的条目不再重复下载（这是"没有附件的"这一需求的边界）
            try:
                atts = self.attachments_of(item_key)
            except ZoteroLocalError as exc:
                LOG.warning("读取附件失败 [%s]：%s", item_key, exc)
                continue

            if atts:
                LOG.debug("已有附件，跳过 [%s] %d 个", item_key, len(atts))
                continue

            out.append(paper)

        return out

    def close(self) -> None:
        """关闭底层 HTTP 客户端。"""
        self._client.close()


def _extract_year(date_text: str) -> str:
    """从 Zotero 的 date 字段里提取 4 位年份。

    在全局中的作用:
        统一年份形态；Zotero 的 date 可能是 '11/2023'、'2024-08-01'、'2023' 等。

    Args:
        date_text: 原始日期字符串

    Returns:
        4 位年份字符串；找不到时返回空串

    Example:
        >>> _extract_year("11/2023")
        '2023'
    """
    import re  # 局部导入：本函数是模块内唯一用正则的地方

    match = re.search(r"(19|20)\d{2}", date_text or "")
    return match.group(0) if match else ""


def _extract_pmcid(data: dict) -> str:
    """Extract a PubMed Central identifier from metadata or Extra."""
    import re
    values = (data.get("PMCID", ""), data.get("pmcid", ""), data.get("extra", ""))
    for value in values:
        match = re.search(r"\bPMC\d+\b", str(value), re.IGNORECASE)
        if match:
            return match.group().upper()
    return ""
