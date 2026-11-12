"""
文件名: state.py
功能: SQLite 幂等状态库，记录每篇论文的终态，避免重复处理
全局作用:
    本文件位于持久化层，是"重复运行安全性"的唯一保障。
    上游：cli 编排时写入各阶段结果
    下游：fetch/attach 在开始前查询，跳过已 attached 的条目
实现效果:
    同一 DOI 第二次运行时：已 attached 的直接跳过；
依赖: 标准库 sqlite3
"""

# ==================== 依赖导入 ====================
# 标准库：SQLite 与时间处理
# 说明: 选 SQLite 而非 JSON 是因为需要按状态并发/原子查询，且可承载失败原因等长文本
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import PaperNeed, Stage
from .safe_files import safe_key

# ==================== 数据库结构 ====================

# 表结构说明（每列的作用在下方 _DDL 注释中逐条给出）
_DDL = """
CREATE TABLE IF NOT EXISTS papers (
    -- Zotero 条目 key，作为主键：一个条目一行
    item_key        TEXT PRIMARY KEY,
    -- DOI（可能为空字符串）；加索引用于按 DOI 快速查重
    doi             TEXT,
    -- 论文标题，便于人工核对状态库
    title           TEXT,
    -- 当前阶段（见 models.Stage）
    stage           TEXT NOT NULL,
    source          TEXT,
    license         TEXT,
    oa_status       TEXT,
    -- 本地 PDF 绝对路径
    file_path       TEXT,
    detail_url      TEXT,
    -- 已挂载到 Zotero 的附件 key
    attachment_key  TEXT,
    -- 失败原因 / 备注（人类可读）
    note            TEXT,
    -- 尝试次数，用于限制重试
    attempts        INTEGER DEFAULT 0,
    -- 首次创建时间
    created_at      TEXT,
    -- 最后更新时间
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_stage ON papers(stage);
"""


class Store:
    """幂等状态库的读写门面。

    在全局中的作用:
        cli 在运行前后调用它记录/查询状态；fetch 与 attach 依据它决定是否跳过。
        这是整个流水线"可安全重复运行"的基础设施。

    Attributes:
        path: SQLite 文件路径

    Example:
        >>> store = Store(Path("data/pipeline.db"))
        >>> store.upsert_need(paper)
        >>> store.get_stage("ABCD1234")
        'pending'
    """

    def __init__(self, path: Path) -> None:
        """初始化并建表。

        目的: 确保数据库文件与其父目录存在，并建好表结构（幂等 DDL）。
        全局作用: 所有实例方法都依赖 self._conn，构造函数必须最先成功。
        实现效果: 首次调用创建 .db 文件；再次调用仅复用。
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：CLI 为单线程，但避免未来在子线程中调用被拦
        if self.path.is_symlink():
            raise ValueError("symlink state DB refused")
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row  # 让查询结果可以按列名访问
        self._conn.executescript(_DDL)
        # Serialize schema checks as well as ALTERs: two processes may open an old DB.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(papers)")}
            for column in ("license", "oa_status", "inbox_matched", "authors", "institutional_authors"):
                if column not in columns:
                    self._conn.execute(f"ALTER TABLE papers ADD COLUMN {column} TEXT")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------
    # 写入接口
    # ------------------------------------------------------------------

    def upsert_need(self, paper: PaperNeed) -> None:
        """登记一条"需要 PDF"的论文（存在则只刷新标题/DOI）。

        在全局中的作用:
            scan 阶段为每个缺失附件的条目调用一次；
            不覆盖已有 stage，避免把已 attached 的记录重置回 pending。

        Args:
            paper: 论文需求对象
        """
        safe_key(paper.item_key)
        now = _now()
        with self._conn:
            self._conn.execute("""INSERT INTO papers
                (item_key,doi,title,stage,attempts,created_at,updated_at,authors,institutional_authors)
                VALUES (?,?,?,?,0,?,?,?,?) ON CONFLICT(item_key) DO UPDATE SET
                doi=excluded.doi,title=excluded.title,updated_at=excluded.updated_at,authors=excluded.authors,
                institutional_authors=excluded.institutional_authors""",
                (paper.item_key,paper.doi,paper.title,Stage.PENDING.value,now,now,json.dumps(paper.authors),
                 str(int(paper.has_institutional_authors))))

    def mark(
        self,
        item_key: str,
        stage: Stage,
        *,
        source: str | None = None,
        license: str | None = None,
        oa_status: str | None = None,
        file_path: str | None = None,
        detail_url: str | None = None,
        attachment_key: str | None = None,
        note: str | None = None,
        bump_attempts: bool = False,
        inbox_matched: bool | None = None,
        authors: list[str] | None = None,
    ) -> None:
        """更新一条记录的状态与附加信息。

        在全局中的作用:
            全流程唯一的写状态入口；各阶段用它推进状态机。

        实现效果:
            只更新传入的非 None 字段，未传字段保持原值（部分更新语义）。

        Args:
            item_key: Zotero 条目 key
            stage: 目标阶段
            source: 下载来源（可选）
            file_path: PDF 路径（可选）
            attachment_key: Zotero 附件 key（可选）
            note: 备注或失败原因（可选）
            bump_attempts: 是否让 attempts 计数 +1
        """
        safe_key(item_key)
        sets = ["stage = ?", "updated_at = ?"]
        vals: list[object] = [stage.value, _now()]

        # ---------- 步骤 1: 按需拼接部分更新字段 ----------
        # 目的: 避免用 None 覆盖掉已有有效值（例如重跑时不该清空已下载路径）
        for column, value in (
            ("source", source),
            ("license", license),
            ("oa_status", oa_status),
            ("file_path", file_path),
            ("detail_url", detail_url),
            ("attachment_key", attachment_key),
            ("note", note),
            ("inbox_matched", None if inbox_matched is None else str(int(inbox_matched))),
            ("authors", None if authors is None else json.dumps(authors)),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                vals.append(value)

        if bump_attempts:
            sets.append("attempts = attempts + 1")

        vals.append(item_key)

        # ---------- 步骤 2: 执行更新 ----------
        # 目的: 落地状态变化；若行不存在（理论上不会）则为无操作
        self._conn.execute(f"UPDATE papers SET {', '.join(sets)} WHERE item_key = ?", vals)
        self._conn.commit()

    def mark_by_doi(self, doi: str, stage: Stage, **kwargs) -> None:
        """按 DOI 更新状态（用于来源链路只有 DOI 没有 item_key 的场景）。

        在全局中的作用:
            供 sources 层的失败记录使用，避免它们需要知道 Zotero 条目 key。

        Args:
            doi: 论文 DOI
            stage: 目标阶段
            **kwargs: 透传给 mark 的可选字段
        """
        rows = self._conn.execute(
            "SELECT item_key FROM papers WHERE doi = ?", (doi,)
        ).fetchall()
        for row in rows:
            self.mark(row["item_key"], stage, **kwargs)

    # ------------------------------------------------------------------
    # 读取接口
    # ------------------------------------------------------------------

    def get(self, item_key: str) -> dict | None:
        """读取一条完整记录。

        Args:
            item_key: Zotero 条目 key

        Returns:
            字段字典；不存在时返回 None
        """
        row = self._conn.execute(
            "SELECT * FROM papers WHERE item_key = ?", (item_key,)
        ).fetchone()
        return dict(row) if row else None

    def get_stage(self, item_key: str) -> str | None:
        """读取当前阶段值。

        Args:
            item_key: Zotero 条目 key

        Returns:
            Stage 的字符串值；不存在时返回 None
        """
        row = self._conn.execute(
            "SELECT stage FROM papers WHERE item_key = ?", (item_key,)
        ).fetchone()
        return row["stage"] if row else None

    def needs_work(self, item_key: str, max_attempts: int = 3) -> bool:
        """判断该条目是否仍需处理（幂等闸门）。

        在全局中的作用:
            全流水线最重要的"防重复"判断：只有返回 True 才允许下载/挂载。

        实现效果:
            - attached（已挂载）与 skipped（主动跳过）→ 永远不再处理
            - failed（失败）→ 仅当尝试次数 < max_attempts 时才重试
              （避免重复处理失败条目）
            - pending / resolved / downloaded → 需要处理（支持断点续跑）

        Args:
            item_key: Zotero 条目 key
            max_attempts: failed 条目的最大重试次数，默认 3

        Returns:
            True 表示仍需处理
        """
        row = self._conn.execute(
            "SELECT stage, attempts FROM papers WHERE item_key = ?", (item_key,)
        ).fetchone()
        if row is None:
            return True  # 新条目

        stage = row["stage"]
        # 终态：已完成或主动跳过，不再触碰
        if stage in {Stage.ATTACHED.value, Stage.SKIPPED.value}:
            return False
        # 失败态：仅在重试预算内才重新尝试
        if stage == Stage.FAILED.value:
            return int(row["attempts"]) < max_attempts
        # 其余中间态一律继续处理
        return True

    def attempts_of(self, item_key: str) -> int:
        """读取尝试次数，用于限制重试上限。

        Args:
            item_key: Zotero 条目 key

        Returns:
            尝试次数；不存在时为 0
        """
        row = self._conn.execute(
            "SELECT attempts FROM papers WHERE item_key = ?", (item_key,)
        ).fetchone()
        return int(row["attempts"]) if row else 0

    def all_rows(self) -> list[dict]:
        """导出全部记录，供报告渲染。

        Returns:
            记录字典列表（按创建时间排序）
        """
        rows = self._conn.execute(
            "SELECT * FROM papers ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def counts_by_stage(self) -> dict[str, int]:
        """按阶段统计数量，用于运行摘要。

        Returns:
            {阶段名: 数量}
        """
        rows = self._conn.execute(
            "SELECT stage, COUNT(*) AS n FROM papers GROUP BY stage"
        ).fetchall()
        return {r["stage"]: r["n"] for r in rows}

    def pending_rows(self, stages: tuple[Stage, ...]) -> list[dict]:
        """取出处于指定阶段的记录。

        在全局中的作用:
            attach 阶段用它捞出"已下载但未挂载"的条目，实现断点续跑。

        Args:
            stages: 关注的阶段集合

        Returns:
            匹配的记录列表
        """
        placeholders = ",".join("?" for _ in stages)
        rows = self._conn.execute(
            f"SELECT * FROM papers WHERE stage IN ({placeholders})",
            [s.value for s in stages],
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        """关闭数据库连接（cli 结束时调用）。"""
        self._conn.close()

    # ------------------------------------------------------------------
    # 调试辅助
    # ------------------------------------------------------------------

    def dump_json(self) -> str:
        """把全库导出为 JSON 字符串（便于排查与人工备份）。

        Returns:
            带缩进的 JSON 文本
        """
        return json.dumps(self.all_rows(), ensure_ascii=False, indent=2, default=str)


def _now() -> str:
    """返回当前时间的 ISO 字符串（本地时区）。

    在全局中的作用:
        统一所有时间戳格式，避免 SQLite 里混入多种时间写法。

    Returns:
        形如 '2026-09-17T18:30:00' 的字符串
    """
    return datetime.now().isoformat(timespec="seconds")


class RunLockedError(RuntimeError):
    pass


class RunLock:
    """Advisory process lock. Kernel releases stale ownership on exit/crash.

    The small persistent lock file is deliberately not unlinked: unlinking would
    allow concurrent processes to lock different inodes with the same name.
    """
    def __init__(self, directory: Path):
        self.path = directory / 'pipeline.lock'
        self.fd = None

    def __enter__(self):
        import fcntl
        import os
        from .safe_files import target
        target(self.path.parent, self.path.name)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise RunLockedError('Another pipeline command holds the state directory lock') from None
        os.ftruncate(self.fd, 0)
        os.write(self.fd, str(os.getpid()).encode('ascii'))
        return self

    def __exit__(self, *args):
        import os
        if self.fd is not None: os.close(self.fd)
