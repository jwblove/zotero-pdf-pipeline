[English](README.md) | [中文](README.zh-CN.md)

# zotero-pdf-pipeline

A validated, auditable, agent-friendly command-line pipeline to find, verify, and attach missing open-access PDFs to Zotero collection items.

v0.1.0 is a first release. Its current verification does not certify universal platform support or production hardening.

### Why this tool?

Most literature fetchers optimize purely for the number of sources. `zotero-pdf-pipeline` prioritizes reliability, verification, and auditability:
- It runs as an external CLI, designed to integrate with automated AI coding agents and local scripts.
- Every candidate PDF undergoes bounded format and identity checks before attachment; uncertain matches require manual review.
- Operations are explicit and tracked in a local SQLite database with redacted core diagnostics and verified-file reuse.

---

## Safety Principles

Implemented boundaries covered by offline regression tests:
- **No direct database access**: Never reads or writes `zotero.sqlite` or files inside Zotero's internal `storage/` directory.
- **Official local interfaces only**: Communicates with Zotero strictly through its local HTTP read API (`http://127.0.0.1:23119`) and the `zotero-mcp` extension (`http://127.0.0.1:23120/mcp`) for writes.
- **Verified attachment**: An item is marked attached only after double-confirmation: one successful MCP import, followed by confirmation that the parent still has a PDF via the local API and an attachment read-back from MCP `get_item_details`. Attachment keys are informational.
- **Mandatory targeting (`--only`)**: Batch commands (`fetch`, `attach`, `run`) require explicit item keys (`--only KEY [KEY ...]`) to restrict downloads and attachments. `run` still scans and registers the entire collection before processing the selected keys.
- **Non-PDF rejection**: Responses must start with the `%PDF-` magic bytes header; HTML error pages are rejected. Redirects are followed only across validated public HTTPS hops (maximum five). Each hostname must resolve exclusively to public addresses; direct connections pin a validated IP with the original Host/TLS SNI and certificate validation. Environment proxies are enabled by default (`[fetch].use_env_proxy = true`) with standard scheme selection and NO_PROXY/no_proxy bypasses. Proxied hops still validate target DNS before every request and redirect, but the proxy can resolve again: DNS rebinding remains a residual risk in proxy mode. Private/loopback target URLs are always rejected, including through a proxy.
- **Validation and quarantine**: Candidates must meet `min_pdf_bytes` and `max_pdf_bytes` (default 100 MiB), PDF magic, parse/page limits and identity checks. On failure, the tool attempts quarantine in `staging-rejected/` beside the staging directory: staging files are moved after a successful copy, external files are copied. Quarantine can fail; the item still fails and is never attached.
- **Idempotent state tracking**: A local SQLite database tracks status (`pending`, `downloaded`, `attached`, `failed`). Attached/skipped items are skipped; failed items have a three-attempt budget. A still-valid `downloaded` file is checked and reused instead of re-downloaded. A kernel lock in the state directory prevents concurrent CLI commands from interleaving and releases ownership after a process exits or crashes.
- **Redacted diagnostics**: Core errors omit response bodies, raw URLs and credential values. Arbitrary plugins must redact their own output; the core cannot control their logging.

---

## Prerequisites: Zotero & zotero-mcp Plugin

Before using this tool, you must configure your local Zotero desktop instance:

1. **Zotero Desktop**: Must be running. In **Edit -> Settings** (or **Preferences** on macOS) **-> Advanced**, check **"Allow other applications on this computer to communicate with Zotero"**.
2. **zotero-mcp Plugin**: Required for attaching files.
   - Download the latest `.xpi` release from [cookjohn/zotero-mcp](https://github.com/cookjohn/zotero-mcp/releases).
   - In Zotero, open **Tools -> Add-ons** (or Plugins), click the gear icon, choose **Install Add-on From File...**, and select the downloaded `.xpi`.
   - Default MCP port: `23120` (Streamable HTTP endpoint: `http://127.0.0.1:23120/mcp`).

> **Testing scope**: Live read, download and attachment have been exercised on WSL with Windows Zotero, including independent MCP read-back. This does not cover every source or platform. The final closeout changes are verified offline; the newest final code has not all been live-regressed.

### Agent-Assisted Setup Prompt

You can ask an AI coding agent (Claude Code, Codex, Hermes, Cursor, etc.) to guide or perform this configuration:

```text
I want to use zotero-pdf-pipeline with my local Zotero desktop. Please guide me through or assist me with installing and configuring the prerequisites:
1. Download the latest zotero-mcp plugin (.xpi) from https://github.com/cookjohn/zotero-mcp/releases and guide me through installing it in Zotero (Tools -> Add-ons -> Install Add-on From File).
2. Ensure Zotero desktop is running and configured to allow local communication (Settings -> Advanced -> "Allow other applications on this computer to communicate with Zotero").
3. Verify that the local HTTP API (http://127.0.0.1:23119/api/users/0) and the MCP endpoint (http://127.0.0.1:23120/mcp) are responsive.
```

---

## Installation & Quick Start

### Installation

Requires Python >= 3.11. Current attachment targets **Windows Zotero**, with PDFs on Windows drives accessible through WSL mounts such as `/mnt/c/...` or `/mnt/d/...`. The converter also recognizes absolute Windows drive paths and normalizes `..`; `/home/...`, other native Linux paths and macOS paths are rejected for attachment. The CLI lock and file helpers use POSIX APIs, so a recognized Windows path does not imply native Windows CLI portability. WSL + Windows Zotero is the exercised setup; native Linux/macOS Zotero attachment is not supported or claimed tested.

```bash
git clone https://github.com/jwblove/zotero-pdf-pipeline.git
cd zotero-pdf-pipeline
pip install -e .
```

### Configuration

Copy the example configuration:

```bash
cp config.example.toml config.toml
```

Edit `config.toml`:
- `[zotero].collection_key`: Your target Zotero collection key.
- For attachment from WSL, set `[paths].archive_dir` and `staging_dir` to Windows-mounted paths, for instance `/mnt/c/zotero-pdf-archive` and `/mnt/c/zotero-pdf-staging`. The example `~/zotero-pdf-archive` is suitable for fetch-only use; change it before attaching.
- `[contact].email`: Contact email required by OpenAlex, Unpaywall, and Crossref APIs. (Can also be set via `ZPP_CONTACT_EMAIL`).

**Finding your collection key**:
- Query the local API in your browser while Zotero is running: `http://127.0.0.1:23119/api/users/0/collections`.
- Or view the collection URL in Zotero Web library (`/collections/<KEY>`).

### Recommended Workflow

Commands that never modify Zotero: `doctor`, `scan`, `fetch`, `report`. Every command loads configuration, creates configured directories and opens/initializes the tool's state DB and lock file; `scan` registers items and `report` writes Markdown. `--help` exits before these side effects.
Commands that **write to Zotero** (via MCP): `attach`, `run`.

1. **Verify environment**:
   ```bash
   zotero-pdf-pipeline doctor
   ```
2. **Scan missing items** (does not modify Zotero; records items in the local state database):
   ```bash
   zotero-pdf-pipeline scan
   ```
3. **Fetch candidate PDFs** (downloads, validates, and archives; does not write to Zotero):
   ```bash
   zotero-pdf-pipeline fetch --free-only --only ITEM_KEY1 ITEM_KEY2
   ```
4. **Attach validated PDFs** (writes attachment via MCP and verifies):
   ```bash
   zotero-pdf-pipeline attach --only ITEM_KEY1 ITEM_KEY2
   ```
5. **Generate report**:
   ```bash
   zotero-pdf-pipeline report
   ```

Global options (`--config PATH`, `--collection KEY`, `-v`/`--verbose`) go **before** the subcommand. Config defaults to `config.toml` in the current working directory; relative configured paths are relative to that file. `ZPP_CONTACT_EMAIL` and `ZPP_PLUGIN_DIR` override their config values.

After attaching, the tool waits up to `[fetch].attach_settle_seconds` (default 20, positive, maximum 60) and then confirms the item still has a PDF attachment. If a file-manager add-on renames/moves/converts the attachment, that is fine; the tool never touches it. **The recorded attachment key is informational** and may change later. Imported files wait the full window even without an add-on; a linked-file PDF can finish after two positive read-backs about two seconds apart within the same window. There is no additional settling window; the final read has a two-second network budget. No key, filename, size, candidate score or count-increase matching is performed, and import is never retried automatically. Selection normally targets items without attachments; if a PDF already exists before an explicit attach, successful import plus continued PDF presence is sufficient and does not prove a new PDF was added. No PDF at the end fails the operation; unavailable/unconfirmed read-back is reported as unverified for manual checking.

To run scan, fetch, attach, and report in a single sequence:
```bash
zotero-pdf-pipeline run --free-only --only ITEM_KEY1
```

---

## Sources & Discovery

Built-in sources execute in the order specified by `[fetch].source_order`. If omitted, the fallback is `["openalex", "unpaywall", "arxiv"]`; the supplied example additionally enables `inbox`, `semantic_scholar`, and `europe_pmc`. `core` and Crossref lookup are off in the example. Available sources:

| Source | Description | Requirements / Identifiers | Default Rate Limit |
|---|---|---|---|
| `inbox` | Local matching against user-provided PDFs | Files in `[paths].inbox_dir` | Local |
| `openalex` | OpenAlex Works API | DOI + `[contact].email` | 1.0 s |
| `unpaywall` | Unpaywall v2 API | DOI + `[contact].email` | 1.0 s |
| `arxiv` | arXiv Atom search API | Title | 3.0 s |
| `semantic_scholar` | Semantic Scholar Graph API | DOI | 1.0 s |
| `europe_pmc` | Europe PMC REST API | DOI or PMCID | 1.0 s |
| `core` (optional) | CORE API v3 | DOI + `CORE_API_KEY` env var | 1.0 s |

Rate limits are per host, within one process, on every request and redirect hop: at least 3 s for each arXiv host, 1 s otherwise. HTTP 429/503 triggers up to two retries with exponential jitter and bounded `Retry-After` (maximum 60 s). Delays over 8 s, an exhausted retry budget or deadline activate a host cooldown (at least 5 s); later items skip it with a diagnostic. This also applies to Semantic Scholar. JSON/XML and HTML bodies are capped at 2 MiB; PDF bodies at `max_pdf_bytes`. Both declared lengths and streamed decoded sizes are checked. Contact email is sent only to OpenAlex, Unpaywall and Crossref; downloads use a generic User-Agent. Referer is query/fragment-free and sent only within the same origin.

Public APIs, downloads and reference HTTP helpers use `HTTPS_PROXY`/`https_proxy` for HTTPS or `ALL_PROXY`/`all_proxy` as fallback, respecting `NO_PROXY`/`no_proxy`; lowercase values take precedence. `HTTP_PROXY`/`http_proxy` follows standard per-scheme selection and does not alone proxy HTTPS. Set `[fetch].use_env_proxy = false` to use pinned direct connections only. Proxy credentials are never included in core diagnostics. Custom plugins using their own clients must manage their routing and secrets responsibly.

All target DNS answers must still be public, including when a proxy or NO_PROXY is used. Synthetic/non-public DNS (including `198.18.0.0/15`) is unsupported and must be corrected outside this application. There is no public resolver-trust switch or private-address bypass.

### Discovery Features
- **Crossref DOI Lookup**: When `enable_crossref = true`, items lacking a DOI are queried against Crossref using title, first author, and year. Found DOIs are used only in memory for the current run; they never alter Zotero records or state DB.
- **PMCID Direct Route**: Europe PMC matches PMCID from item metadata or `Extra`. Direct PDF download (`https://europepmc.org/articles/{pmcid}?pdf=render`) is attempted only after open-access metadata is confirmed.
- **Landing Page Discovery**: Parses HTML `<meta name="citation_pdf_url">` on open-access landing pages (OpenAlex, Unpaywall, Europe PMC). Candidate targets must still return `%PDF-` and pass validation.
- **License Recording**: OpenAlex, Unpaywall, and Europe PMC metadata record `license` and `oa_status` in state reports. An empty value indicates no license was recorded by the API, not a legal status determination.
- **Hit Rate Notice**: Open-access sources work well for OA papers and preprints. The hit rate on paywalled or subscription journals without institutional access is low.

---

## The Inbox Workflow

For papers obtained legitimately through institutional subscriptions, interlibrary loans, or direct author requests:
1. Place the PDF files into the configured `inbox_dir` (default: `data/inbox`).
2. Run `fetch` (with `inbox` in `source_order`).
3. The tool extracts text from inbox PDFs and matches them against missing items by DOI or title similarity (>= `title_similarity`).
4. Exactly one match must be found; ambiguous files are skipped.
5. Only regular, non-symlink `.pdf` files (case-insensitive) within `max_pdf_bytes` are scanned. Inbox originals are preserved (copied to staging/archive, never deleted). A readable image-only PDF may match an explicit DOI/title in its filename (a DOI slash may be percent-encoded or replaced with an underscore/hyphen); this exception is recorded with a note. Otherwise it needs manual review.

Validation parses at most the first two pages, rejects invalid or excessive page counts (over 20,000), and runs pypdf in a subprocess with a 10 s timeout (on POSIX also 5 s CPU and 768 MiB address-space limits). Parser errors or text extraction failures on all inspected pages fail closed; a single inspected page that fails text extraction is retained with empty text. Blank scans are rejected except for the explicit inbox match above. At least 20 normalized characters are required for a text-based decision. Title comparison removes known arXiv/journal banners and compares bounded windows among the first 40 usable lines (Unicode dashes, ligatures, accents, case and punctuation normalized; token and sequence scores). The configured threshold is unchanged. Exact DOI or first-author surname can support only a borderline score of at least 0.35 with substantive shared title tokens; these alone never accept an unrelated paper.

An otherwise-passing result below 0.95 similarity needs manual review and is quarantined if none of the item author surnames appears in the first 60 nonempty lines of the **first page** (second-page references do not count). Name matching normalizes NFKC/case/diacritics and PDF accent-spacing artifacts. The negative author rule applies only with at least 120 normalized characters and 20 words in that region. It is skipped for absent/unusable creator names, near-exact titles (>= 0.95), institutional/single-field creators, collaboration banners, or an explicit inbox DOI/title match. A matching DOI alone cannot override this author mismatch. This is a heuristic, not proof of identity. Missing pypdf is a blocking `doctor` failure and prevents validation.

---

## Plugins

The minimal [Source contract](docs/PLUGIN_PROTOCOL.md) requires only a name and a `fetch` method. For a working local-copy plugin, save these ten lines as `~/.config/zotero-pipeline/plugins/local_copy.py` and place a matching PDF named `candidate.pdf` beside it:

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

Replace these values in your existing config sections (do not append duplicate sections); keep your real collection key configured:

```toml
[fetch]
source_order = []
[plugins]
directory = "~/.config/zotero-pipeline/plugins"
enabled = ["local_copy"]
```

Select the matching item, fetch, review the result, then attach:

```bash
zotero-pdf-pipeline --config config.toml fetch --only ITEM_KEY --plugin local_copy
zotero-pdf-pipeline --config config.toml attach --only ITEM_KEY
```

The original is retained and ordinary PDF validation still applies. This example makes no source HTTP requests. [PLUGIN_PROTOCOL.md](docs/PLUGIN_PROTOCOL.md) distinguishes the minimal contract from optional helpers, gives callable `request`/`write_bytes` signatures, and explains platform limits.

### Plugin Rules & Discovery
- **Locations**: File in `~/.config/zotero-pipeline/plugins/<NAME>.py` (or `ZPP_PLUGIN_DIR`), or a Python package entry point in group `zotero_pdf_pipeline.sources`.
- **Explicit Opt-in**: Plugins must be listed in `[plugins].enabled` in config AND requested via `--plugin NAME` on the CLI. `--plugin NAME` is repeatable; built-ins run first. Combining it with `--free-only` is a CLI error. `--free-only` never loads plugins or reads CORE credentials.
- **Identical Pipeline**: Plugin outputs pass through the exact same size, magic byte, and title validation before attachment.
- **Security**: Plugins execute arbitrary Python code with your local permissions. Only install plugins from trusted sources.
- **Agent Workflow**: Give [docs/PLUGIN_PROTOCOL.md](docs/PLUGIN_PROTOCOL.md) to your coding agent and ask it to write a plugin for any repository or library service you have authorized access to.

Use global `-v` before `fetch`/`run` for redacted plugin exception traces (type, module and line numbers only; no message, source, path or URL). A plugin returning `None` is identified by name. See the protocol for discovery naming and class rules.

### Example Plugin: Sci-Hub Illustration
The repository includes an educational example in [examples/plugins/scihub.py](examples/plugins/scihub.py) to illustrate the `Source` protocol:
- It is disabled by default.
- No mirror address is bundled; the user must supply `ZPP_SCIHUB_BASE_URL` (must be a valid HTTPS URL with no credentials, queries, or fragments).
- It uses the same public HTTPS/redirect and size guards, including proxy selection (direct hops are pinned), self-throttles at least 5 seconds per request, and stops on any challenge (including PDF candidates). It stops immediately on HTTP 429/503 and honours bounded Retry-After through the host cooldown and never solves CAPTCHAs or handles logins.
- Use may be unlawful in certain jurisdictions; the user assumes sole legal responsibility. The project does not host, endorse, or encourage its use.
- Core source support is limited to the listed APIs and inbox. Users are responsible for reviewing any plugins they install.

---

## Credentials & Secret Management

- Core code never stores, prompts for, or writes credentials to disk.
- Plugins and the optional CORE source obtain credentials exclusively via environment variables (e.g., `CORE_API_KEY`). Contact email is not an API key and may be configured in `[contact].email` or `ZPP_CONTACT_EMAIL`.
- **Recommended Practice**: Inject credentials at runtime using an existing secrets manager or CLI injection tool (e.g., 1Password CLI `op run --`, Doppler, Bitwarden Secrets Manager, Infisical) instead of writing secrets into configuration files or shell scripts. Never commit secrets to version control or paste them into AI agent chat prompts.

---

## Responsible Use & Disclaimer

- This software is intended strictly for personal study, academic research, and collection maintenance.
- Users must comply with publisher terms of service, applicable copyright law, and data provider API policies.
- The repository does not bundle or distribute literature. Running the tool saves PDFs locally in staging/archive/quarantine; you are responsible for those copies.
- The core pipeline does not bypass paywalls, access controls, or CAPTCHA challenges.
- Provided "as is" under the MIT License without warranty of any kind.

---

## Comparison with Similar Tools

In-Zotero JavaScript extensions:
- [Zotero built-in "Find Available PDF"](https://www.zotero.org)
- [zotfetch](https://github.com/fdossi/zotfetch)
- [zotero-zotadata](https://github.com/ydeng11/zotero-zotadata)
- [FullVahti](https://github.com/heidihelena/fullvahti)
- [PDFerret](https://github.com/urschrei/pdferret)

**When to use in-Zotero plugins**: If you prefer managing downloads entirely within the Zotero GUI and want broader community-scraped sources with a single click.
**When to use zotero-pdf-pipeline**: If you want an external, inspectable CLI designed for automated AI coding agent workflows, deterministic validation and quarantine to protect library integrity, auditable SQLite tracking, and safe inbox ingestion.

---

## Development

Install development dependencies:
```bash
pip install -e '.[test]'
```

Run the test suite offline (mocked HTTP, no live network required):
```bash
python -m pytest tests -q
```

### Project Layout
- `src/zotero_pdf_pipeline/`: Core CLI, sources, validation, quarantine, attacher, MCP and local API clients.
- `tests/`: Offline test suite.
- `examples/plugins/`: Reference plugin implementations.
- `docs/`: Protocol documentation for plugin authors.
- `.github/workflows/tests.yml`: CI testing across Python 3.11 and 3.12.

### Contributing
Pull requests improving validation, open-access integrations, or bug fixes are welcome. Site-specific paywall bypass or shadow-library implementations will not be accepted into core.

---

## License

[MIT](LICENSE) (c) jwblove
