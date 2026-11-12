# Source plugin protocol

## Minimal Source contract

A file `NAME.py` exports `source`, an instance or zero-argument class with:

- `name: str`, matching `[A-Za-z][A-Za-z0-9_]*` and the requested `NAME`.
- `fetch(item_meta, dest_dir) -> pathlib.Path | None`: return a candidate PDF path, or `None` when unavailable. An exception means failure; core diagnostics record its type, not its message.

`item_meta` is a `PaperNeed`: `item_key`, `title`, `doi`, `authors` (an ordered surname list; single-field institutional names may appear), `year`, `item_type`, `publication`, `collection_key`, `pmcid`, and `has_institutional_authors` (boolean, default false). String/list metadata may be empty. `PaperNeed` removes HTML and collapses whitespace in `title`; recognized DOI forms lose URL/`doi:` prefixes and trailing punctuation and become lowercase. Handle empty/unrecognized identifiers. A discovered DOI is temporary; never change Zotero metadata.

Write only inside `dest_dir`. Credentials come only from environment variables and must never be printed. Plugins execute arbitrary Python with user permissions; the core is not a sandbox.

`fetch` copies, validates and archives candidates; it does not attach them. Failed validation attempts quarantine and always blocks attachment, even if quarantine fails. A separate `attach` command imports once, waits, then confirms that the parent still has a PDF using local API plus MCP read-back. Add-on renames/moves/key changes are fine; the recorded key is informational.

## Working local-copy quick start

Save these ten lines as `~/.config/zotero-pipeline/plugins/local_copy.py`. Put a matching, legitimately obtained PDF named `candidate.pdf` beside it:

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

In your existing `config.toml`, replace the corresponding values (do not duplicate TOML sections). Set your collection key as described in the README:

```toml
[fetch]
source_order = []
[plugins]
directory = "~/.config/zotero-pipeline/plugins"
enabled = ["local_copy"]
```

Select the item that matches this PDF:

```bash
zotero-pdf-pipeline --config config.toml fetch --only ITEM_KEY --plugin local_copy
# After reviewing the fetch result:
zotero-pdf-pipeline --config config.toml attach --only ITEM_KEY
```

This uses the Zotero local API for metadata and makes no source HTTP requests. The original PDF is retained; ordinary size/title/author validation still applies. `source_order = []` avoids built-ins running first during this example.

## Loading and opt-in

File discovery uses `[plugins].directory`, default `~/.config/zotero-pipeline/plugins`, overridden by `ZPP_PLUGIN_DIR`. Alternatively publish an entry point named `NAME` in group `zotero_pdf_pipeline.sources` that exports the instance/class. A name must resolve to exactly one file or entry point; missing names and duplicates/collisions are rejected. File modules are cached for the process; restart the CLI after editing them.

Both `[plugins].enabled` and `--plugin NAME` are required. Repeat `--plugin` for several sources; built-ins run first. `--free-only` conflicts with `--plugin` and never loads plugins or reads credentialed source keys.

## Optional network and file helpers

The interface above does not require these helpers. For a network plugin, this is an actual helper call; pass an authorized HTTPS URL from your source lookup:

```python
from zotero_pdf_pipeline.public_http import request
from zotero_pdf_pipeline.safe_files import write_bytes

def download_pdf(url, item_meta, dest_dir):
    maximum = 10 * 1024 * 1024
    response = request(url, timeout=30, max_bytes=maximum)
    if not response.content.startswith(b"%PDF-"): return None
    return write_bytes(dest_dir, item_meta.item_key + ".pdf",
                       response.content, max_bytes=maximum)
```

Supported public calls (the helpers are optional):

```python
request(url, timeout=45, *, max_bytes=2097152, headers=None, params=None)  # -> httpx.Response
write_bytes(root: Path, name: str, content: bytes, **kwargs)             # -> Path
copy_file(source: Path, root: Path, name: str, *, max_bytes=104857600)    # -> Path
```

For `write_bytes`, supported keyword arguments are `max_bytes=104857600` and `unique=True`. Existing names get a suffix; `unique=False` refuses a collision. Use the returned path. These helpers rely on OS file APIs such as directory file descriptors, no-follow flags and hard links; portability is not automatic. The current CLI lock uses POSIX APIs. Test the destination platform; using `pathlib` alone does not make a plugin portable.

`request` returns a bounded, materialized `httpx.Response`, not a lazy stream. `timeout` is seconds; `max_bytes` limits Content-Length and actual decoded bytes. `headers=` and `params=` are optional. The default cap is 2 MiB; choose an explicit PDF cap as above. Non-200 responses, invalid URLs, size limits and connection errors raise redacted `PublicHTTPError`; do not log raw third-party exceptions or URLs. Use `request`, rather than calling the client factory directly, so every URL and redirect is guarded.

The helper limits redirects to five and retries HTTP 429/503 at most twice, with bounded Retry-After, backoff and process-local host cooldown. HTML/JSON limits also apply. `copy_file` and `write_bytes` publish exclusively inside a root, reject symlink targets and do not overwrite existing files.

During CLI `fetch`/`run`, helpers inherit `[fetch].use_env_proxy` (default true). No plugin-side policy call is needed. Custom clients must implement the same URL checks, bounded requests and proxy policy themselves.

### DNS and proxy limits

- HTTPS DNS answers must all be globally routable. Direct connections pin the first validated address with original Host/SNI and certificate validation; each redirect is revalidated.
- Standard scheme-specific proxies and NO_PROXY/no_proxy are respected. NO_PROXY selects routing and never disables URL validation. A proxy may resolve the target again, so downstream DNS rebinding remains a residual risk.
- Literal/private/non-public targets and non-public DNS answers, including synthetic `198.18.0.0/15` answers, are unsupported. Correct DNS/network routing outside the application; there is no public application bypass or special resolver compatibility mode.
- Userinfo, single-label names and localhost/.localhost/.local/.internal names are rejected. An environment proxy does not override these checks.

## Checklist for agent-authored plugins

Paste this document to your coding agent and ask it to implement the intended authorized source. Review:

- Correct `Source` export, missing-field behavior and metadata matching; no Zotero metadata writes.
- Environment-only credentials, redacted output, writes confined to `dest_dir`, bounded requests/files and parser work.
- Public HTTPS and per-redirect checks; proxy/NO_PROXY behavior; no private-address or synthetic-DNS bypass.
- Self-throttling and Retry-After; stop at authentication or challenges. Never automate CAPTCHA solving, login handling or bypasses.
- Offline fake DNS/transports/clocks covering success, no candidate, non-PDF, oversized content, private redirects, rate limits and explicit fake-IP trust boundaries.
- Normal validation may require manual review when titles match but no item author occurs on the first page; documented institutional/collaboration/inbox exceptions apply.

Install only trusted code after reviewing its behavior and applicable terms.

## Debugging a plugin

Use global `-v` before `fetch`/`run`. Exception traces contain the plugin name, exception type, modules and line numbers; no exception messages, source, paths or URLs. A `None` result is identified by plugin name. Plugins remain responsible for their own logs.
