[English](README.md) | [中文](README.zh-CN.md)

# zotero-pdf-pipeline

一个可校验、可审计且适配 Agent 工作流的命令行工具，用于查找、验证并将缺失的开放获取（OA）PDF 附件挂载到 Zotero 集合条目。

v0.1.0 为首次发布，当前验证不代表跨平台通用支持或生产安全认证。

### 为什么选择本工具？

大多数文献获取工具单纯追求来源数量的堆叠。`zotero-pdf-pipeline` 将可靠性、质量验证与审计能力放在首位：
- 采用外部命令行运行，专为 AI 编程 Agent（Coding Agent）与本地脚本自动化编排设计。
- 所有候选 PDF 在挂载前均须通过有资源上限的格式与身份校验；无法可靠匹配的文件需人工复核。
- 流程显式可控，通过本地 SQLite 记录状态与核心脱敏诊断，并复用已验证的下载文件。

---

## 安全原则

以下是实现并纳入离线回归的安全边界：
- **绝不直接读写数据库**：绝不直接访问或修改 `zotero.sqlite`，也绝不触碰 Zotero 的 `storage/` 内部目录。
- **仅使用官方本地接口**：仅通过 Zotero 本地 HTTP API（`http://127.0.0.1:23119`）执行只读查询，通过 `zotero-mcp` 插件（`http://127.0.0.1:23120/mcp`）执行写入。
- **严格回读确认**：MCP 成功导入一次后，由本地 API 确认父条目仍有 PDF，并通过 MCP `get_item_details` 回读确认附件存在。附件 key 仅供参考。
- **强制定向处理（`--only`）**：`fetch`、`attach`、`run` 等批处理命令强制要求显式指定条目键（`--only KEY [KEY ...]`），限制下载与挂载范围。`run` 仍会先扫描并登记整个集合，再处理所选条目。
- **拒收非 PDF 响应**：响应内容必须以 `%PDF-` 魔数开头，拦截 HTML 错误页。重定向仅沿逐跳校验后的公共 HTTPS 地址跟随，最多五跳；域名解析结果须全部为公共地址，直连时固定到已校验 IP，并保留原 Host/TLS SNI 与证书验证。默认启用环境代理（`[fetch].use_env_proxy = true`），按标准协议选择并遵守 NO_PROXY/no_proxy。代理模式仍在每次请求和重定向前检查目标 DNS，但代理可能再次解析，因此存在剩余 DNS 重绑定风险。即使经代理，也始终拒绝私网/回环目标 URL。
- **校验失败立即隔离**：候选文件须满足 `min_pdf_bytes` 与 `max_pdf_bytes`（默认 100 MiB）、PDF 魔数、解析/页数上限及身份校验。失败时尝试隔离到暂存目录旁的 `staging-rejected/`：暂存文件成功复制后移除，外部文件只复制。隔离可能失败，但该条目仍记失败且绝不挂载。
- **幂等状态库**：基于本地 SQLite 记录条目生命周期（`pending`、`downloaded`、`attached`、`failed`），重复执行时跳过 attached/skipped 条目；failed 最多尝试三次。已有且仍通过校验的 downloaded 文件会直接复用，避免重新下载。状态目录内的内核文件锁阻止 CLI 命令交错执行，进程退出或崩溃后自动释放锁占用。
- **敏感信息全面脱敏**：核心错误不包含响应正文、原始 URL 或凭据值。任意第三方插件须自行脱敏，核心无法控制其日志输出。

---

## 前置依赖：Zotero 与 zotero-mcp 插件

在运行本流水线前，请确保本地 Zotero 桌面端已完成以下配置：

1. **Zotero 桌面客户端**：保持运行。在 **编辑 -> 设置**（macOS 为 **偏好设置**）**-> 高级** 中，勾选 **“允许其他应用与 Zotero 通信”**。
2. **zotero-mcp 插件**：用于文件挂载与通信。
   - 前往 [cookjohn/zotero-mcp](https://github.com/cookjohn/zotero-mcp/releases) 下载最新的 `.xpi` 安装包。
   - 在 Zotero 中点击 **工具 -> 附加组件**（或插件），点击右上角齿轮图标，选择 **Install Add-on From File...** 并选中下载的 `.xpi` 文件。
   - 默认 MCP 端口为 `23120`（Streamable HTTP 端点：`http://127.0.0.1:23120/mcp`）。

> **验证范围**：已在 WSL + Windows Zotero 上实测读取、下载和挂载，并通过独立 MCP 回读确认。这不覆盖所有来源或平台。本轮最终收尾改动以离线验证为准，不能据此宣称最新最终代码已全部完成实机回归。

### AI Agent 辅助安装提示词

你可以直接将以下提示词发送给 AI 编程助手（如 Claude Code、Codex、Hermes、Cursor 等）协助完成配置：

```text
我想在本地 Zotero 桌面端上配合使用 zotero-pdf-pipeline。请指导或协助我完成以下前置配置：
1. 从 https://github.com/cookjohn/zotero-mcp/releases 下载最新的 zotero-mcp 插件（.xpi），并指导我在 Zotero（工具 -> 附加组件 -> Install Add-on From File）中完成安装。
2. 确保 Zotero 桌面端正在运行，并开启本地通信权限（设置 -> 高级 -> 勾选“允许其他应用与 Zotero 通信”）。
3. 协助验证本地 HTTP API（http://127.0.0.1:23119/api/users/0）与 MCP 端点（http://127.0.0.1:23120/mcp）是否能正常响应。
```

---

## 安装与快速上手

### 环境安装

要求 Python >= 3.11。当前挂载面向 **Windows Zotero**，PDF 须位于 Windows 盘，并可通过 `/mnt/c/...`、`/mnt/d/...` 等 WSL 挂载路径访问。转换器也识别绝对 Windows 盘符路径并规范化 `..`，但挂载时拒绝 `/home/...`、其他原生 Linux 路径及 macOS 路径。CLI 锁与文件 helper 使用 POSIX API，能识别 Windows 路径不意味着原生 Windows CLI 自动可移植。已实测组合为 WSL + Windows Zotero；不支持或宣称已测试原生 Linux/macOS Zotero 挂载。

```bash
git clone https://github.com/jwblove/zotero-pdf-pipeline.git
cd zotero-pdf-pipeline
pip install -e .
```

### 配置文件

复制示例配置：

```bash
cp config.example.toml config.toml
```

编辑 `config.toml`：
- `[zotero].collection_key`：目标 Zotero 集合的 key。
- 从 WSL 挂载时，将 `[paths].archive_dir` 和 `staging_dir` 设为 Windows 挂载路径，如 `/mnt/c/zotero-pdf-archive` 和 `/mnt/c/zotero-pdf-staging`。示例中的 `~/zotero-pdf-archive` 适用于仅获取文件，挂载前须修改。
- `[contact].email`：联系邮箱，OpenAlex、Unpaywall 和 Crossref API 要求提供（亦可通过环境变量 `ZPP_CONTACT_EMAIL` 设置）。

**如何获取 Collection Key**：
- 在 Zotero 运行时，浏览器访问本地 API 查看所有集合：`http://127.0.0.1:23119/api/users/0/collections`。
- 或在 Zotero Web 端个人文库的 URL 中查看（`/collections/<KEY>`）。

### 推荐执行流程

各命令对 Zotero 的影响：
- **不修改 Zotero 的命令**：`doctor`、`scan`、`fetch`、`report`。每个命令都会加载配置、创建配置目录并打开/初始化本工具的状态库及锁文件；`scan` 登记条目，`report` 写 Markdown 文件。`--help` 在产生这些副作用之前退出。
- **写操作命令（修改 Zotero）**：`attach`、`run`。

1. **环境自检**：
   ```bash
   zotero-pdf-pipeline doctor
   ```
2. **扫描缺附件条目**（不修改 Zotero，登记至本地 SQLite 状态库）：
   ```bash
   zotero-pdf-pipeline scan
   ```
3. **下载候选文件**（下载、校验并归档，不修改 Zotero）：
   ```bash
   zotero-pdf-pipeline fetch --free-only --only ITEM_KEY1 ITEM_KEY2
   ```
4. **挂载附件**（调用 MCP 挂载并通过本地 API 校验）：
   ```bash
   zotero-pdf-pipeline attach --only ITEM_KEY1 ITEM_KEY2
   ```
5. **生成统计报告**：
   ```bash
   zotero-pdf-pipeline report
   ```

全局选项（`--config PATH`、`--collection KEY`、`-v`/`--verbose`）须放在**子命令之前**。默认配置是当前工作目录的 `config.toml`；配置中的相对路径基于该配置文件。`ZPP_CONTACT_EMAIL` 与 `ZPP_PLUGIN_DIR` 优先于对应配置值。

挂载后，工具最多等待 `[fetch].attach_settle_seconds`（默认 20 秒，必须为正，最大 60 秒），再确认条目仍有 PDF 附件。文件管理类附加组件对附件改名、移动或转换均不影响成功判断，工具绝不干预这些附件。**记录的附件 key 仅供参考**，之后仍可能变化。即使没有附加组件，imported_file 也等待完整窗口；linked_file PDF 在同一窗口内约间隔两秒的两次回读均存在时，可提前完成。不另加稳定等待窗口，最终回读的网络时间预算为两秒。不匹配 key、文件名、大小，不做候选评分，也不要求附件数量增加，绝不自动重试导入。通常只选择缺附件条目；显式挂载前若已有 PDF，导入成功后仍有 PDF 即可确认，并不证明新增了一个 PDF。窗口结束时没有 PDF 则失败；无法取得或确认回读时记为未验证，交由人工检查。

若要一次性执行扫描、下载、挂载并生成报告，可使用 `run` 命令：
```bash
zotero-pdf-pipeline run --free-only --only ITEM_KEY1
```

---

## 检索来源与发现机制

内置来源按 `[fetch].source_order` 列表顺序依次尝试。省略时回退为 `["openalex", "unpaywall", "arxiv"]`；所附示例另启用了 `inbox`、`semantic_scholar` 和 `europe_pmc`。示例中 `core` 与 Crossref 补全默认关闭。可选来源如下：

| 来源名称 | 描述 | 所需标识 / 前置条件 | 默认请求间隔 |
|---|---|---|---|
| `inbox` | 本地收件箱匹配 | `[paths].inbox_dir` 目录内的 PDF | 本地处理 |
| `openalex` | OpenAlex Works API | DOI + `[contact].email` | 1.0 秒 |
| `unpaywall` | Unpaywall v2 API | DOI + `[contact].email` | 1.0 秒 |
| `arxiv` | arXiv Atom 搜索 API | Title | 3.0 秒 |
| `semantic_scholar` | Semantic Scholar Graph API | DOI | 1.0 秒 |
| `europe_pmc` | Europe PMC REST API | DOI 或 PMCID | 1.0 秒 |
| `core`（可选） | CORE API v3 | DOI + 环境变量 `CORE_API_KEY` | 1.0 秒 |

限流以进程内的主机为单位，每次请求与重定向跳转均计入：每个 arXiv 主机至少间隔 3 秒，其他主机 1 秒。HTTP 429/503 最多重试两次，使用带抖动的指数退避，并遵守有界的 `Retry-After`（最多 60 秒）。等待超过 8 秒、重试耗尽或触及期限时，启用至少 5 秒的主机冷却；后续条目会跳过该主机并给出诊断。Semantic Scholar 同样适用。JSON/XML 与 HTML 响应上限为 2 MiB，PDF 为 `max_pdf_bytes`，同时检查声明长度与实际流式解码大小。联系邮箱仅发送给 OpenAlex、Unpaywall 与 Crossref，下载采用通用 User-Agent。Referer 去除查询参数/锚点，且仅同源发送。

公共 API、下载和参考 HTTP helper 对 HTTPS 使用 `HTTPS_PROXY`/`https_proxy`，否则回退到 `ALL_PROXY`/`all_proxy`，并遵守 `NO_PROXY`/`no_proxy`；小写变量优先。`HTTP_PROXY`/`http_proxy` 按标准规则仅用于对应协议，单独设置它不会代理 HTTPS。设置 `[fetch].use_env_proxy = false` 可只走固定 IP 的直连。核心诊断不会包含代理凭据。使用自有客户端的第三方插件须自行负责路由及凭据处理。

无论是否使用代理或 NO_PROXY，目标 DNS 答案都必须为公网地址。合成/非公网 DNS（含 `198.18.0.0/15`）不受支持，须在应用外修正；没有公开的解析器信任开关或私网地址绕过选项。

### 辅助发现特性
- **Crossref DOI 补全**：配置 `enable_crossref = true` 时，无 DOI 的条目会使用标题、一作及年份向 Crossref 查询。查得的 DOI 仅在当次运行的内存中使用，不写入 Zotero 元数据或状态库。
- **PMCID 直连路径**：Europe PMC 支持从条目元数据或 `Extra` 字段识别 PMCID。仅在元数据明确确认为 open access 后，才尝试直连渲染下载（`https://europepmc.org/articles/{pmcid}?pdf=render`）。
- **落地页元数据嗅探**：支持解析 OpenAlex、Unpaywall 和 Europe PMC 开放获取落地页 HTML 中的 `<meta name="citation_pdf_url">` 标签。所获取的文件同样必须具备 `%PDF-` 头并经完整校验。
- **许可证记录**：来自 OpenAlex、Unpaywall 和 Europe PMC 的元数据会提取 `license` 与 `oa_status` 记录在状态库与报告中。空值仅表示来源未返回有效记录，不代表法律意义上的授权界定。
- **命中率说明**：开放获取源对 OA 论文及预印本效果良好；对于尚未提供开放访问的付费订阅期刊，公开网络直接获取的成功率天然较低。

---

## 收件箱（Inbox）工作流

对于通过机构订阅、文献传递或向作者索取等合法途径获得的文献：
1. 将 PDF 文件放入配置的 `inbox_dir`（默认：`data/inbox`）。
2. 执行 `fetch`（确保 `source_order` 中包含 `inbox`）。
3. 工具提取收件箱 PDF 文本层，按 DOI 或标题相似度（>= `title_similarity`）与缺失条目匹配。
4. 必须存在且仅存在唯一匹配结果；匹配存在歧义的文件将被跳过。
5. 仅扫描体积不超过 `max_pdf_bytes` 的普通非符号链接 `.pdf` 文件（扩展名不区分大小写）。原文件予以保留，复制至暂存/归档区而不删除。可正常解析的无文本扫描件允许通过文件名中的显式 DOI/标题匹配（DOI 斜杠可百分号编码，或替换为下划线/连字符），并记录例外说明；否则需人工复核。

校验最多解析前两页，拒绝无效或过多页数（超过 20,000 页），并在有 10 秒超时的子进程中运行 pypdf（POSIX 下另限制 5 秒 CPU 和 768 MiB 地址空间）。解析错误或所检查页面全部提取失败时拒绝文件。空白扫描件也拒绝，除上述显式收件箱匹配例外。基于文本的判断至少需要 20 个规范化字符。标题比较去除已知 arXiv/期刊页眉，并在前 40 个可用文本行的有界窗口中选最佳匹配，统一 Unicode 连字符、连字、重音、大小写与标点，结合词集合及序列分数。配置阈值保持不变；正文其他位置的 DOI 或第一作者姓氏只能辅助分数至少 0.35 且有实质共享标题词的边界情况。首页前 120 行中以 `DOI:` 或 `https://doi.org/` 开头标注的唯一 DOI 如与目标 DOI 精确匹配，即使 Zotero 题目或作者不一致也可通过，但会发出元数据复核警告；首页标注的 DOI 不一致或存在多个 DOI 时拒绝并隔离。首页 DOI 仍可能属于引用，不能替代人工核对。

若标题相似度低于 0.95，其他校验虽通过，但在**首页**前 60 个非空行中未找到任何条目作者姓氏，则记为需人工复核并隔离，第二页引用中的作者不算。姓名比较统一 NFKC、大小写、重音及 PDF 重音错位造成的空格。该负向作者规则仅在此区域至少有 120 个规范化字符且有 20 个词时启用；缺少可用作者名、近乎完全相同的标题（>= 0.95）、机构/单字段作者、合作组署名或显式收件箱 DOI/标题匹配均跳过。唯一的首页标注 DOI 精确匹配时可覆盖作者不符，并提示人工核对元数据；其他位置的 DOI 不能覆盖。这是启发式校验，并非身份的严格证明。缺少 pypdf 会导致 `doctor` 失败并阻止文件验证。

---

## 插件系统

最小 [Source 协议](docs/PLUGIN_PROTOCOL.md) 只要求名称和 `fetch` 方法。以下十行是可工作的本地复制插件：保存为 `~/.config/zotero-pipeline/plugins/local_copy.py`，并在旁边放置与目标条目匹配的 `candidate.pdf`：

```python
from pathlib import Path
from zotero_pdf_pipeline.safe_files import copy_file

class LocalCopy:
    name = "local_copy"
    def fetch(self, item_meta, dest_dir):
        original = Path(__file__).with_name("candidate.pdf")
        if not original.is_file(): return None
        return copy_file(original, dest_dir, item_meta.item_key + ".pdf")
source = LocalCopy()
```

在现有配置节中替换以下值，不要追加重复的配置节，并保持真实集合 key 已配置：

```toml
[fetch]
source_order = []
[plugins]
directory = "~/.config/zotero-pipeline/plugins"
enabled = ["local_copy"]
```

选择匹配的条目，先获取并检查结果，再挂载：

```bash
zotero-pdf-pipeline --config config.toml fetch --only ITEM_KEY --plugin local_copy
zotero-pdf-pipeline --config config.toml attach --only ITEM_KEY
```

原文件保留，仍执行常规 PDF 校验；此示例不请求在线来源。[PLUGIN_PROTOCOL.md](docs/PLUGIN_PROTOCOL.md) 区分最小协议与可选 helper，并提供 `request`/`write_bytes` 可调用签名及平台限制。

### 插件规范与加载规则
- **存放路径**：位于 `~/.config/zotero-pipeline/plugins/<NAME>.py`（或 `ZPP_PLUGIN_DIR`），或通过 entry point 分组 `zotero_pdf_pipeline.sources` 注册。
- **显式启用**：插件必须在配置文件的 `[plugins].enabled` 中声明，且在 CLI 中通过 `--plugin NAME` 显式调用。`--plugin NAME` 可重复，内置来源先执行。与 `--free-only` 同用会报 CLI 参数错误；`--free-only` 不加载插件，也不读取 CORE 凭据。
- **统一质量关卡**：插件返回的文件同样必须通过体积、文件头、标题相似度等全套校验，否则进入隔离区。
- **安全性提示**：加载插件即以当前用户权限执行任意 Python 代码，请务必只使用可信代码。
- **Agent 工作流**：直接将 [docs/PLUGIN_PROTOCOL.md](docs/PLUGIN_PROTOCOL.md) 提交给你的 Coding Agent，让它为你拥有合法访问权限的校内文库或机构仓储编写对接插件。

在 `fetch`/`run` 前加全局 `-v` 可查看插件异常的脱敏调用栈：仅异常类型、模块和行号，不含消息、源码、路径或 URL。插件返回 `None` 时也会标明插件名。命名与类导出规则见协议文档。

### 示例插件：Sci-Hub 教学演示
仓库在 [examples/plugins/scihub.py](examples/plugins/scihub.py) 中包含一个只用于演示 `Source` 协议的范例：
- 默认处于禁用状态。
- 代码中不包含任何镜像地址；用户必须自行配置环境变量 `ZPP_SCIHUB_BASE_URL`（必须为不带凭据、参数或锚点的合规 HTTPS 地址）。
- 使用相同的公共 HTTPS/逐跳地址校验及大小限制，并支持代理选择（直连时固定 IP），请求间隔至少 5 秒；任何人机挑战（包括 PDF 候选响应）均会立即终止。遇 HTTP 429/503 立即停止，通过主机冷却遵守有界 Retry-After，绝不解验证码或处理登录。
- 在部分国家或地区使用可能不符合当地法律，由此产生的法律后果完全由用户自行承担；本项目不托管、不背书、不鼓励其使用。
- 核心来源仅限上述 API 与收件箱，用户须自行审核安装的任何插件。

---

## 凭据与安全管理

- 核心流水线代码绝不持久化存储、提示输入或将凭据落盘。
- 插件及可选 CORE 来源仅通过环境变量（如 `CORE_API_KEY`）读取凭据。联系邮箱不是 API 密钥，可在 `[contact].email` 或 `ZPP_CONTACT_EMAIL` 中设置。
- **推荐做法**：使用现有的密钥管理工具（如 1Password CLI `op run --`、Doppler、Bitwarden Secrets Manager、Infisical）在运行时注入环境变量，避免将密钥写入配置文件或 shell 脚本。切勿将密钥提交到版本控制仓库，也绝不要直接粘贴在 Agent 对话中。

---

## 合规使用与免责声明

- 本工具仅供个人学习、学术研究及个人文库维护使用。
- 使用者应自觉遵守出版商服务条款、版权法律法规以及各数据接口使用规范。
- 仓库不附带或分发文献。运行工具会在暂存、归档、隔离目录中保存 PDF，用户须对这些本地副本负责。
- 核心流水线代码不包含任何绕过付费墙、登录认证或人机验证的逻辑。
- 软件基于 MIT 许可证按“现状”（AS IS）提供，不包含任何明示或暗示的担保。

---

## 与同类工具对比

Zotero 生态内的 JavaScript 插件：
- [Zotero 内置“查找可用 PDF”](https://www.zotero.org)
- [zotfetch](https://github.com/fdossi/zotfetch)
- [zotero-zotadata](https://github.com/ydeng11/zotero-zotadata)
- [FullVahti](https://github.com/heidihelena/fullvahti)
- [PDFerret](https://github.com/urschrei/pdferret)

**何时选用上述插件**：希望完全在 Zotero 图形界面内操作，偏好社区规则并追求单次点击的最大下载成功率。
**何时选用 zotero-pdf-pipeline**：需要外部命令行以便与 AI Coding Agent 自动化编排集成、需要严格的文件校验与隔离以保护文库干净、或需要可审计的本地 SQLite 状态追踪与无损收件箱归档。

---

## 开发与贡献

安装测试依赖：
```bash
pip install -e '.[test]'
```

离线运行测试套件（使用模拟 HTTP，无需外网连接）：
```bash
python -m pytest tests -q
```

### 项目结构
- `src/zotero_pdf_pipeline/`：核心 CLI、来源模块、校验、隔离、挂载器、MCP 与本地 API 客户端。
- `tests/`：离线测试套件。
- `examples/plugins/`：插件参考实现。
- `docs/`：面向插件作者的开发协议说明。
- `.github/workflows/tests.yml`：针对 Python 3.11 与 3.12 的自动化 CI 测试。

### 参与贡献
欢迎提交关于内容校验、开放获取源对接及问题修复的 Pull Request。任何针对特定站点的破解或绕过代码均不会被合并入核心库。

---

## 开源许可

[MIT](LICENSE) (c) jwblove
