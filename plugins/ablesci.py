"""AbleSci request and download adapter."""

from __future__ import annotations

import http.client
import http.cookiejar
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from zotero_pdf_pipeline.models import PaperNeed, normalize_doi
from zotero_pdf_pipeline.naming import build_filename, sanitize_filename

LOG = logging.getLogger("pipeline.ablesci")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

class AbleSciError(RuntimeError):
    """科研通操作失败异常。"""

@dataclass
class PaperMeta:
    """科研通返回的文献元数据。"""

    title: str = ""
    doi: str = ""
    journal: str = ""
    year: str = ""
    publish_at: str = ""

@dataclass
class FetchOutcome:
    """一次科研通抓取的结果。"""

    pdf_path: Path
    detail_url: str

class AbleSciClient:
    """科研通 HTTP 客户端。"""

    def __init__(self, base: str, email: str, password: str, timeout: int = 45) -> None:
        """初始化客户端。"""
        self.base = base.rstrip("/")
        self.email = email
        self.password = password
        self.timeout = timeout

        self.cookies = http.cookiejar.CookieJar()

        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )
        self.csrf: str | None = None

        self._last_lookup_at: float = 0.0

        self.lookup_min_interval: float = 4.0

    def _request(
        self,
        url: str,
        data: dict | None = None,
        ajax: bool = False,
        referer: str | None = None,
        method: str | None = None,
    ):
        """发起一次 HTTP 请求（统一头部与错误处理）。"""
        if url.startswith("/"):
            url = f"{self.base}{url}"

        headers = {"User-Agent": USER_AGENT}
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        if referer:
            headers["Referer"] = referer

        body = None
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = urllib.parse.urlencode(data).encode("utf-8")

        request = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            return self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise AbleSciError(f"HTTP {exc.code} {url} {detail}") from exc
        except urllib.error.URLError as exc:
            raise AbleSciError(f"网络错误 {url}：{exc.reason}") from exc
        except (http.client.RemoteDisconnected, ConnectionResetError, TimeoutError, OSError) as exc:

            raise AbleSciError(f"连接中断 {url}：{type(exc).__name__}") from exc

    def _get_text(self, url: str, **kwargs) -> str:
        """GET 并解码为文本。"""
        return self._request(url, **kwargs).read().decode("utf-8", "replace")

    def _post_json(self, url: str, data: dict, **kwargs) -> dict:
        """POST 并解析 JSON 响应。"""
        raw = self._request(url, data=data, **kwargs).read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AbleSciError(f"响应不是 JSON（{url}）：{raw[:200]}") from exc

    def _refresh_csrf(self, page_url: str) -> str:
        """从页面 meta 标签中提取 CSRF token。"""
        html = self._get_text(page_url)
        match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
        if not match:

            if 'id="login2-form"' in html or "/site/login" in html:
                raise AbleSciError("会话已失效（被重定向到登录页），请重新登录")
            raise AbleSciError(f"页面中未找到 CSRF token：{page_url}")
        self.csrf = match.group(1)
        return self.csrf

    def login(self) -> bool:
        """登录科研通。"""

        html = self._get_text("/site/login")
        match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
        if not match:
            raise AbleSciError("登录页未找到 CSRF token，站点结构可能已变化")
        token = match.group(1)

        result = self._post_json(
            "/site/login",
            {
                "_csrf": token,
                "email": self.email,
                "password": self.password,
                "remember": "1",
            },
            ajax=True,
            referer=f"{self.base}/site/login",
        )

        if result.get("code") != 0:
            msg = str(result.get("msg", ""))

            if "验证码" in msg:
                raise AbleSciError("登录需要图形验证码，请先在浏览器手动登录一次以通过风控")
            raise AbleSciError(f"登录失败：{msg[:200]}")

        LOG.info("科研通登录成功")
        return True

    def is_logged_in(self) -> bool:
        """检查当前 Cookie 会话是否仍然有效。"""
        try:
            html = self._get_text("/assist/create")
            return 'id="login2-form"' not in html and "/site/login" not in html
        except AbleSciError:
            return False

    def point_balance(self) -> int:
        """读取当前积分余额。"""
        try:
            html = self._get_text("/")
            match = re.search(r'id="user-point-now">(\d+)<', html)
            return int(match.group(1)) if match else -1
        except AbleSciError:
            return -1

    def lookup(self, doi: str, retries: int = 2) -> PaperMeta | None:
        """用 DOI 查询科研通的文献元数据（"智能提取"）。"""
        for attempt in range(retries + 1):

            if self._last_lookup_at:
                elapsed = time.monotonic() - self._last_lookup_at
                if elapsed < self.lookup_min_interval:
                    wait = self.lookup_min_interval - elapsed
                    LOG.debug("智能提取节流：等待 %.1f 秒", wait)
                    time.sleep(wait)

            meta = self._lookup_once(doi)
            self._last_lookup_at = time.monotonic()

            if meta is not None:
                return meta

            if attempt < retries:
                backoff = 6.0 * (attempt + 1)
                LOG.info("科研通未识别 %s，%.0f 秒后重试（%d/%d）", doi, backoff, attempt + 1, retries)
                time.sleep(backoff)

        return None

    def _lookup_once(self, doi: str) -> PaperMeta | None:
        """执行一次智能提取（不含节流与重试）。"""

        referer = f"{self.base}/assist/create"
        try:
            token = self._refresh_csrf("/assist/create")
        except AbleSciError as exc:
            LOG.error("无法打开发布页：%s", exc)
            return None

        try:
            result = self._post_json(
                "/assist/onekey-query",
                {"_csrf": token, "onekey": doi},
                ajax=True,
                referer=referer,
            )
        except AbleSciError as exc:
            LOG.warning("智能提取请求失败：%s", exc)
            return None

        data = result.get("data")
        if isinstance(data, dict) and data.get("title"):
            return self._meta_from_dict(data, doi)

        job_uuid = result.get("job_uuid")
        if job_uuid:
            meta = self._poll_lookup(job_uuid, token, referer)
            if meta:
                return meta

        msg = str(result.get("msg", ""))
        if "onekey-query-not-found" in msg or result.get("code") == 1:
            LOG.warning("科研通未能识别该 DOI：%s", doi)
        else:
            LOG.warning("智能提取返回异常：%s", msg[:200])
        return None

    def _meta_from_dict(self, data: dict, doi: str) -> PaperMeta:
        """把站点返回的 data 字典转换成 PaperMeta。"""
        year = data.get("year") or ""
        return PaperMeta(
            title=str(data.get("title", "")).strip(),
            doi=str(data.get("doi") or doi).strip(),
            journal=str(data.get("journal") or data.get("publication") or "").strip(),
            year=str(year)[:4],
            publish_at=str(data.get("publish_at") or ""),
        )

    def _poll_lookup(self, job_uuid: str, token: str, referer: str) -> PaperMeta | None:
        """轮询异步查询任务。"""
        deadline = time.monotonic() + 45

        while time.monotonic() < deadline:
            time.sleep(1.5)
            try:
                status = self._post_json(
                    "/assist/paper-lookup-status",
                    {"_csrf": token, "job_uuid": job_uuid},
                    ajax=True,
                    referer=referer,
                )
            except AbleSciError as exc:
                LOG.debug("轮询失败：%s", exc)
                continue

            state = status.get("status")
            if state == "succeeded" or (status.get("code") == 0 and status.get("data")):
                return self._meta_from_dict(status.get("data") or {}, "")
            if state == "failed":
                LOG.warning("科研通查询任务失败：%s", str(status.get("msg"))[:150])
                return None
        LOG.warning("科研通查询任务超时（45 秒）")
        return None

    def my_assists(self) -> list[dict]:
        """解析"我的求助"列表。"""
        html = self._get_text("/my/assist-my")
        out: list[dict] = []

        for row_match in re.finditer(r'<tr class="list-item">(.*?)</tr>', html, re.S):
            row = row_match.group(1)

            link = re.search(r'/assist/detail\?id=(\w+)"[^>]*>([^<]*)</a>', row)
            if not link:
                continue
            assist_id = link.group(1)
            title = _strip_tags(link.group(2)).strip()

            status_match = re.search(r'assist-badge[^>]*>([^<]+)<', row)
            status = status_match.group(1).strip() if status_match else ""
            time_match = re.search(r'class="list-time">([^<]+)<', row)
            created_at = time_match.group(1).strip() if time_match else ""

            out.append(
                {
                    "id": assist_id,
                    "title": title,
                    "status": status,

                    "finished": ("已完结" in status)
                    or ("已关闭" in status)
                    or ("assist-badge-finished" in row)
                    or ("assist-badge-closed" in row),
                    "created_at": created_at,
                }
            )

        LOG.debug("我的求助解析到 %d 条", len(out))
        return out

    def unfinished_count(self) -> int:
        """统计未完结求助数量。"""
        return sum(1 for item in self.my_assists() if not item["finished"])

    def publish(self, doi: str, meta: PaperMeta | None = None) -> str:
        """发布一条文献求助。"""
        referer = f"{self.base}/assist/create"

        token = self._refresh_csrf("/assist/create")

        form = {
            "_csrf": token,
            "Assist[doi]": doi,
            "Assist[title]": (meta.title if meta else "") or doi,
            "Assist[url]": f"https://doi.org/{doi}",
            "Assist[type]": "1",
            "Assist[point]": "10",
            "Assist[note]": "",
            "Assist[remark]": "",
            "Assist[suppl]": "0",
        }

        result = self._post_json("/assist/create", form, ajax=True, referer=referer)

        if result.get("code") != 0:
            msg = str(result.get("msg", ""))

            if "未完结" in msg or "请先处理" in msg:
                raise AbleSciError(f"已有未完结求助，需先处理：{msg[:150]}")
            if "积分" in msg:
                raise AbleSciError(f"积分不足：{msg[:150]}")
            raise AbleSciError(f"发布失败：{msg[:250]}")

        data = result.get("data") or {}
        assist_id = ""
        if isinstance(data, dict):
            assist_id = str(data.get("id") or data.get("assist_id") or "")

        if not assist_id:
            LOG.info("发布响应未含 id，从「我的求助」定位新条目")
            assist_id = self._find_assist_id_by_doi(doi, meta.title if meta else "")

        if not assist_id:
            raise AbleSciError("发布可能成功但未能定位详情页，请人工检查科研通『我的求助』")

        detail_url = f"{self.base}/assist/detail?id={assist_id}"
        LOG.info("已发布求助")
        return detail_url

    def _find_assist_id_by_doi(self, doi: str, title: str = "") -> str:
        """在"我的求助"中定位刚发布的求助 id。"""
        items = self.my_assists()

        if title:
            want = _normalize_for_match(title)
            for item in items:
                if _normalize_for_match(item["title"]) == want:
                    LOG.info("按标题定位到求助：%s", item["id"])
                    return item["id"]

            for item in items:
                got = _normalize_for_match(item["title"])
                if len(got) > 25 and (want.startswith(got) or got.startswith(want[:25])):
                    LOG.info("按标题前缀定位到求助：%s", item["id"])
                    return item["id"]

        if doi:
            html = self._get_text("/my/assist-my")
            for item in items:

                try:
                    detail = self.detail_html(item["id"])
                except AbleSciError:
                    continue
                if doi.lower() in detail.lower():
                    LOG.info("按 DOI 定位到求助：%s", item["id"])
                    return item["id"]

        return ""

    def detail_html(self, detail_url: str) -> str:
        """获取详情页 HTML。"""
        if detail_url.startswith("http"):
            return self._get_text(detail_url)
        return self._get_text(f"/assist/detail?id={detail_url}")

    def is_finished(self, detail_url: str) -> bool:
        """判断求助是否已完结。"""
        try:
            html = self.detail_html(detail_url)
        except AbleSciError as exc:
            LOG.debug("读取详情页失败：%s", exc)
            return False
        return "assist-badge-finished" in html

    def assist_is_closed(self, detail_url: str) -> bool:
        """判断求助是否已被关闭。"""

        try:
            html = self.detail_html(detail_url)
        except AbleSciError:
            return False

        if "assist-badge-closed" in html:
            return True

        if "如需该文献，请重新发布求助" in html:
            return True

        match = re.search(r"id=([A-Za-z0-9]+)", detail_url)
        if not match:
            return False
        assist_id = match.group(1)
        try:
            for item in self.my_assists():
                if item["id"] == assist_id:
                    return item.get("status") == "已关闭"
        except AbleSciError:
            pass
        return False

    def available_file(self, detail_url: str) -> dict | None:
        """检查详情页是否已有可下载的应助文件。"""
        html = self.detail_html(detail_url)

        file_id = ""
        for pattern in (
            r'value="([A-Za-z0-9]+)"\s+class="assist-file-id"',
            r'class="assist-file-id"[^>]*value="([A-Za-z0-9]+)"',
            r'name="assist_file_id"[^>]*value="([A-Za-z0-9]+)"',
        ):
            found = re.search(pattern, html)
            if found:
                file_id = found.group(1)
                break

        download_url = ""
        download_match = re.search(r'href="(/assist/download\?id=[A-Za-z0-9]+)"', html)
        if download_match:
            download_url = download_match.group(1)

        if not file_id and not download_url:
            return None

        if not file_id and download_url:
            id_match = re.search(r"id=([A-Za-z0-9]+)", download_url)
            if id_match:
                file_id = id_match.group(1)

        filename = ""
        name_match = re.search(
            r'href="/assist/download\?id=[A-Za-z0-9]+"[^>]*>([^<]+)</a>', html
        )
        if name_match:
            filename = name_match.group(1).strip()

        return {
            "file_id": file_id,
            "download_url": download_url,
            "filename": filename,
        }

    def download_file(self, detail_url: str, file_info: dict, dest: Path) -> Path:
        """下载应助文件到指定路径。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        referer = detail_url

        download_path = file_info.get("download_url") or ""
        if not download_path:
            file_id = file_info.get("file_id", "")
            download_path = f"/assist/download?id={file_id}" if file_id else ""
        if not download_path:
            raise AbleSciError("缺少下载地址信息，无法下载")

        page = self._request(download_path, referer=referer).read().decode("utf-8", "replace")
        cfg_match = re.search(r"const\s+config\s*=\s*(\{.*?\});", page, re.S)
        if not cfg_match:
            raise AbleSciError("下载页未包含 config，站点结构可能已变化")
        try:
            dl_config = json.loads(cfg_match.group(1))
        except json.JSONDecodeError as exc:
            raise AbleSciError(f"解析下载页 config 失败：{exc}") from exc

        hashid = str(dl_config.get("hashid") or "")
        token_url = str(dl_config.get("tokenUrl") or "/file/request-download-token")
        servers = dl_config.get("normalServers") or [{"id": "2", "name": "线路1"}]
        LOG.debug("下载页 config：hashid=%s 预期大小=%s", hashid, dl_config.get("expectedSize"))

        last_error = ""
        attempts: list[tuple[str, str]] = []

        attempts.append(("highspeed", "0"))
        attempts.append(("highspeed", "0"))

        for server in servers:
            attempts.append(("normal", str(server.get("id") or "2")))

        for channel, server_id in attempts:
            try:
                token_data = self._request_download_token(
                    token_url, hashid, server_id, referer, channel=channel
                )
            except AbleSciError as exc:
                last_error = str(exc)
                LOG.debug("%s 通道申请 token 失败：%s", channel, exc)
                continue

            host = str(token_data.get("host") or "")
            token = str(token_data.get("token") or "")
            if not host or not token:
                last_error = f"{channel} 通道未返回有效 host/token"
                continue

            sep = "&" if "?" in host else "?"
            file_url = f"{host}{sep}token={urllib.parse.quote(token)}"

            if not self._is_allowed_host(file_url, dl_config):
                last_error = f"下载节点未在允许列表内：{host}"
                LOG.warning("%s", last_error)
                continue

            LOG.info("使用 %s 通道下载（节点：%s）", channel, host.split("/")[2] if "/" in host else host)

            try:
                resp = self.opener.open(
                    urllib.request.Request(
                        file_url,
                        headers={"User-Agent": USER_AGENT, "Referer": f"{self.base}{download_path}"},
                    ),
                    timeout=max(self.timeout, 180),
                )

                expected = int(dl_config.get("expectedSize") or 0)
                written = 0
                first = b""
                last_progress = time.monotonic()
                stall_timeout = 30
                with dest.open("wb") as fh:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        if not first:
                            first = chunk[:5]
                        fh.write(chunk)
                        written += len(chunk)

                        now = time.monotonic()
                        if now - last_progress > stall_timeout:
                            raise TimeoutError(f"下载停滞（已收 {written} 字节）")
                        last_progress = now

                if not first.startswith(b"%PDF-"):
                    dest.unlink(missing_ok=True)
                    last_error = f"返回内容不是 PDF（{written} 字节）"
                    LOG.debug("%s：%s", last_error, file_url[:120])
                    continue

                if expected and written < expected * 0.95:
                    dest.unlink(missing_ok=True)
                    last_error = f"下载不完整（{written}/{expected} 字节）"
                    LOG.warning("线路 %s %s", server_id, last_error)
                    continue

                LOG.info(
                    "已下载：%s（%d 字节，%s 通道）", dest.name, written, channel
                )
                return dest
            except (TimeoutError, OSError) as exc:

                dest.unlink(missing_ok=True)
                last_error = f"读取超时/中断（{type(exc).__name__}）"
                LOG.warning("线路 %s 下载中断：%s", server_id, exc)
                continue
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}"
                continue
            except urllib.error.URLError as exc:
                last_error = f"网络错误 {exc.reason}"
                continue

        raise AbleSciError(
            f"未能下载到 PDF（hashid={hashid}）。最后错误：{last_error}。"
            "可能原因：文件已被服务器删除（完结超 24 小时）或线路全部拥挤。"
        )

    def _request_download_token(
        self,
        token_url: str,
        hashid: str,
        server_id: str,
        referer: str,
        channel: str = "normal",
    ) -> dict:
        """向站点申请一次性下载 token。"""

        token = self._refresh_csrf(referer)
        is_high = channel == "highspeed"
        payload = {
            "_csrf": token,
            "type": "assistFile",
            "id": hashid,
            "channel": channel,

            "highspeed": "1" if is_high else "0",
            "fallback": "0",

            "file_server": "0" if is_high else server_id,
            "remove_sensitive": "1",
        }
        result = self._post_json(token_url, payload, ajax=True, referer=referer)
        if result.get("code") not in (0, None) and not result.get("host"):
            raise AbleSciError(f"申请下载 token 失败：{str(result.get('msg', ''))[:150]}")

        data = result.get("data") if isinstance(result.get("data"), dict) else result
        return data or {}

    def _is_allowed_host(self, file_url: str, dl_config: dict) -> bool:
        """校验下载地址是否在站点允许的节点列表内。"""
        allowed = {h.lower() for h in (dl_config.get("allowedNodeHosts") or [])}
        if not allowed:
            return True
        try:
            parsed = urllib.parse.urlparse(file_url)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()

        if parsed.netloc == urllib.parse.urlparse(self.base).netloc and parsed.path == "/file/download":
            return True
        return host in allowed

    def robot_gave_up(self, detail_url: str) -> bool:
        """判断站点机器人是否已放弃抓取（只能等人工应助）。"""
        try:
            html = self.detail_html(detail_url)
        except AbleSciError as exc:
            LOG.debug("读取详情页失败：%s", exc)
            return False

        return "机器人已退出" in html or "未找到该文献" in html

    def accept(self, detail_url: str, file_id: str, note: str = "感谢") -> bool:
        """采纳应助文件，使求助完结。"""

        token = self._refresh_csrf(detail_url)

        result = self._post_json(
            "/assist/file-handle",
            {
                "_csrf": token,
                "assist_file_id": file_id,
                "note": note,
                "type": "accept",
            },
            ajax=True,
            referer=detail_url,
        )

        if result.get("code") != 0:
            raise AbleSciError(f"采纳失败：{str(result.get('msg', ''))[:200]}")

        LOG.info("已采纳应助文件，求助完结")
        return True

    def close_assist(self, detail_url: str, note: str, assist_id: str = "") -> bool:
        """关闭一条求助（收回悬赏积分）。"""

        if not assist_id:
            match = re.search(r"id=([A-Za-z0-9]+)", detail_url)
            assist_id = match.group(1) if match else ""
        if not assist_id:
            raise AbleSciError(f"无法从 {detail_url} 提取求助 id")

        token = self._refresh_csrf(detail_url)
        result = self._post_json(
            "/assist/close",
            {"_csrf": token, "id": assist_id, "note": note},
            ajax=True,
            referer=detail_url,
        )
        if result.get("code") != 0:
            raise AbleSciError(f"关闭求助失败：{str(result.get('msg', ''))[:200]}")

        LOG.info("已关闭求助 %s（理由：%s）", assist_id, note)
        return True

    def fetch_paper(
        self,
        paper: PaperNeed,
        staging_dir: Path,
        initial_wait_seconds: int,
        poll_seconds: int,
        max_monitor_seconds: int,
        resume_detail_url: str | None = None,
        authors: list[str] | None = None,
        on_published: "Callable[[str], None] | None" = None,
        give_up_seconds: int = 180,
    ) -> FetchOutcome:
        """对一篇论文执行"发布 → 监控 → 下载 → 采纳"全流程。"""

        if resume_detail_url:
            detail_url = resume_detail_url
            LOG.info("沿用既有求助")
        else:

            pending = self.unfinished_count()
            if pending > 0:
                raise AbleSciError(
                    f"科研通有 {pending} 条未完结求助，无法发布新求助。"
                    "请先在网站上处理（采纳或撤销）后重试。"
                )

            balance = self.point_balance()
            if 0 <= balance < 12:
                raise AbleSciError(f"科研通积分不足（当前 {balance} 分，单篇预算 12 分）")
            LOG.info("科研通积分余额：%s", balance if balance >= 0 else "未知")

            meta = self.lookup(paper.doi) if paper.doi else None
            if meta and meta.title:
                LOG.info("站点识别标题：%s", meta.title[:70])
            detail_url = self.publish(paper.doi, meta)

            if on_published:
                on_published(detail_url)

        LOG.info("等待 %d 秒后开始检查应助情况", initial_wait_seconds)
        time.sleep(initial_wait_seconds)

        give_up_at = time.monotonic() + give_up_seconds
        deadline = time.monotonic() + max_monitor_seconds
        downloaded: Path | None = None

        while time.monotonic() < deadline:

            if time.monotonic() >= give_up_at:
                raise AbleSciError(
                    f"无人应助（发布后 {give_up_seconds} 秒内未出现文件）。"
                    f"详情页：{detail_url}"
                )

            if self.is_finished(detail_url):
                LOG.info("求助已完结，尝试直接从详情页取文件")
            info = self.available_file(detail_url)

            if info:

                filename = build_filename(
                    authors=list(authors or []),
                    year=paper.year,
                    title=paper.title,
                    suffix=".pdf",
                )
                dest = staging_dir / filename
                try:
                    downloaded = self.download_file(detail_url, info, dest)
                except AbleSciError as exc:
                    LOG.warning("下载失败，稍后重试：%s", exc)
                    downloaded = None

                if downloaded:
                    try:
                        self.accept(detail_url, info["file_id"])
                    except AbleSciError as exc:

                        LOG.warning("自动采纳失败（文件已下载）：%s", exc)
                    return FetchOutcome(pdf_path=downloaded, detail_url=detail_url)

            LOG.info(
                "%s 尚未发现可下载文件，%d 秒后重试（放弃倒计时 %.0f 秒）",
                time.strftime("%H:%M:%S"),
                poll_seconds,
                max(0.0, give_up_at - time.monotonic()),
            )

            remaining = give_up_at - time.monotonic()
            time.sleep(max(5, min(poll_seconds, remaining)))

        raise AbleSciError(
            f"监控超时（{max_monitor_seconds // 3600} 小时）仍未出现可下载文件。"
            f"详情页：{detail_url}"
        )

def _strip_tags(text: str) -> str:
    """去除 HTML 标签与多余空白。"""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text or "")).strip()

def _normalize_for_match(text: str) -> str:
    """把标题归一化为可直接比较的形式。"""
    import unicodedata

    plain = unicodedata.normalize("NFKC", _strip_tags(text)).lower()

    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", plain)

def make_client(cfg_ablesci, email: str, password: str) -> AbleSciClient:
    """按配置构造 AbleSciClient 的工厂函数。"""
    return AbleSciClient(cfg_ablesci.base_url, email, password)

import os
import tempfile
import stat
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

_CLIENT = None
_LOCK = threading.RLock()
_STATE_PATH = Path(os.environ.get("ZPP_ABLESCI_STATE_DIR") or (Path.home() / ".local" / "state" / "zotero-pipeline")) / "ablesci.json"
_PUBLISHED = 0
# Budget unit per new request: 10 points to publish + 2 for the high-speed download channel (site-charged amount prevails).
_POINTS_PER_REQUEST = 12

class _Redact(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        client = _CLIENT
        for secret in (getattr(client, "email", ""), getattr(client, "password", "")):
            if secret:
                message = message.replace(secret, "[redacted]")
        message = re.sub(r"https?://[^\s]+", "[url]", message)
        record.msg, record.args = message, ()
        return True

LOG.addFilter(_Redact())

def _client():
    """Create/login once per process. Credentials come only from env vars."""
    global _CLIENT
    with _LOCK:
        if _CLIENT is None:
            email = os.environ.get("ABLESCI_EMAIL") or os.environ.get("ABLESCI_USER", "")
            password = os.environ.get("ABLESCI_PASSWORD") or os.environ.get("ABLESCI_PASS", "")
            if not email or not password:
                raise AbleSciError("missing ABLESCI_EMAIL/ABLESCI_PASSWORD")
            client = AbleSciClient("https://www.ablesci.com", email, password)
            _CLIENT = client  # Redaction must also cover login diagnostics.
            try:
                if not client.is_logged_in():
                    client.login()
            except Exception:
                _CLIENT = None
                raise AbleSciError("AbleSci authentication failed") from None
        return _CLIENT

def _state_key(paper):
    return json.dumps([paper.item_key, (paper.doi or "").strip().lower()], ensure_ascii=False, separators=(",", ":"))

def _read_state():
    try:
        with _STATE_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("invalid state")
        return data
    except FileNotFoundError:
        return {}

def _write_state(data):
    _STATE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=_STATE_PATH.parent, prefix=".ablesci-")
    try:
        os.fchmod(fd, 0o600)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            os.close(fd)
            LOG.error("状态文件无法设置为 600，停止操作")
            raise OSError("state file permissions cannot be enforced")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, _STATE_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def _matching_assist(client, paper):
    title = _normalize_for_match(paper.title or "")
    doi = normalize_doi(paper.doi or "")
    matches = []
    for item in client.my_assists():
        if item.get("finished"):
            continue
        item_title = _normalize_for_match(item.get("title") or "")
        item_doi = normalize_doi(item.get("doi") or "")
        if item_doi and doi and item_doi != doi:
            continue
        if (title and title == item_title) or (doi and doi in (normalize_doi(item.get("title") or ""), item_doi)):
            assist_id = str(item.get("id") or "")
            if re.fullmatch(r"[A-Za-z0-9_]+", assist_id):
                matches.append(assist_id)
    if len(matches) != 1:
        return None
    return "https://www.ablesci.com/assist/detail?id=" + matches[0]

@contextmanager
def _limit_highspeed(client, state, key):
    """Reserve the one chargeable attempt before invoking the legacy client."""
    original = getattr(client, "_request_download_token", None)
    if original is None:
        yield
        return
    had_override = "_request_download_token" in vars(client)
    previous = vars(client).get("_request_download_token")

    def request(token_url, hashid, server_id, referer, channel="normal"):
        if channel == "highspeed":
            entry = state.get(key)
            if not isinstance(entry, dict) or not entry.get("detail_url"):
                raise OSError("cannot reserve highspeed attempt without request state")
            if entry.get("highspeed_attempt_reserved"):
                raise AbleSciError("高速通道本次求助已尝试，转用普通通道")
            entry["highspeed_attempt_reserved"] = True
            # Failure propagates out of legacy retry handling before any token request.
            _write_state(state)
        return original(token_url, hashid, server_id, referer, channel=channel)

    client._request_download_token = request
    try:
        yield
    finally:
        if had_override:
            client._request_download_token = previous
        else:
            del client._request_download_token

class _AbleSciSource:
    temporary_candidates = True
    name = "ablesci"

    def fetch(self, item_meta, dest_dir):
        global _PUBLISHED
        if not item_meta.doi:
            return None
        with _LOCK:
            detail_url = None
            try:
                client = _client()
                key = _state_key(item_meta)
                state = _read_state()
                detail_url = state.get(key, {}).get("detail_url")
                if not detail_url and client.unfinished_count() > 0:
                    detail_url = _matching_assist(client, item_meta)
                    if not detail_url:
                        LOG.warning("已有未完结求助，但无法唯一匹配当前文献；不作处理")
                        return None
                    state[key] = {"detail_url": detail_url, "timestamp": datetime.now(timezone.utc).isoformat()}
                    _write_state(state)
                    LOG.info("接管匹配的未完结求助")
                if not detail_url:
                    # Verify the filesystem enforces 0600 before spending points.
                    _write_state(state)
                    max_points = int(os.environ.get("ZPP_ABLESCI_MAX_POINTS_PER_RUN", str(_POINTS_PER_REQUEST)))
                    if _PUBLISHED * _POINTS_PER_REQUEST + _POINTS_PER_REQUEST > max_points:
                        LOG.warning("科研通本进程发布积分预算已用尽")
                        return None
                def published(url):
                    nonlocal detail_url
                    global _PUBLISHED
                    detail_url = url
                    _PUBLISHED += 1
                    state[key] = {"detail_url": url, "timestamp": datetime.now(timezone.utc).isoformat()}
                    _write_state(state)
                with _limit_highspeed(client, state, key):
                    outcome = client.fetch_paper(
                        item_meta, Path(dest_dir), 60, 60, 3600,
                        resume_detail_url=detail_url, on_published=published,
                        authors=list(item_meta.authors or []),
                        give_up_seconds=int(os.environ.get("ZPP_ABLESCI_GIVE_UP_SECONDS", "180")))
                state.pop(key, None)
                _write_state(state)
                return outcome.pdf_path
            except Exception as exc:
                if isinstance(exc, AbleSciError) and "无人应助" in str(exc) and detail_url:
                    LOG.info("无人应助，尝试关闭求助")
                    try:
                        closed = client.close_assist(detail_url, "暂时无人应助")
                        if closed:
                            state.pop(key, None)
                            _write_state(state)
                            LOG.info("关闭求助成功")
                        else:
                            LOG.warning("关闭求助未确认成功，保留续传状态")
                    except Exception:
                        LOG.warning("关闭求助或保存状态失败，需核查")
                    return None
                LOG.warning("科研通操作失败（%s）", type(exc).__name__)
                return None

source = _AbleSciSource()
