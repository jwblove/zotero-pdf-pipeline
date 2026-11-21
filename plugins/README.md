# 可选文献来源 / Optional sources

在仓库根目录使用配置文件时，可按下例修改已有的 `[plugins]` 段。每次运行还须用 `--plugin` 选择来源。目录相对于配置文件解析。

Windows 命令使用 `.\pipeline.cmd` 替代下文的 `zotero-pdf-pipeline`。

With a config file in the repository root, set its plugin directory and enable the adapters you intend to use. Built-in sources run first.

```toml
[plugins]
directory = "plugins"
enabled = ["scidownl"]
```

```sh
zotero-pdf-pipeline --config config.toml fetch --only ITEM_KEY --plugin scidownl
```

| Plugin | 配置和行为 / Configuration and effects |
| --- | --- |
| `scidownl` | 解析页面的 `#pdf[src]`。可用 `ZPP_SCIHUB_BASE_URLS` 指定按序尝试的 HTTPS 来源（最多三个），或用 `ZPP_SCIHUB_BASE_URL` 指定一个。The built-in mirror list is used when both are unset. Verification or rate limiting stops further attempts. [Upstream viewer convention](https://github.com/Tishacy/SciDownl/tree/3e66abd) |
| `scihub` | 需设置 `ZPP_SCIHUB_BASE_URL`。默认使用 HTTP 客户端。Browser mode is optional and requires Playwright and an installed Chromium or Firefox. Set `ZPP_SCIHUB_BROWSER=1` to enable it. Session cookies persist in the dedicated browser profile at `~/.local/state/zotero-pipeline/scihub-profile`. |
| `flaresolverr` | 需自行启动本机 FlareSolverr 服务。The default endpoint is `http://127.0.0.1:8191/v1` (override with `ZPP_FLARESOLVERR_URL`). It opens the DOI landing page by default. Set `ZPP_FLARESOLVERR_BASE_URLS` to supply up to three HTTPS targets. Returned cookies and user agent are used for the candidate download. |
| `ablesci` | 通过环境变量 `ABLESCI_EMAIL` 和 `ABLESCI_PASSWORD` 登录。插件能发布求助、花费积分、下载后自动采纳；采纳发生在核心校验之前。The process budget defaults to 12 points for one new request. Set `ZPP_ABLESCI_MAX_POINTS_PER_RUN` to change it. The provider determines actual charges. |

AbleSci 将续传记录写入 `~/.local/state/zotero-pipeline/ablesci.json`（Windows 使用当前账户专用 ACL，POSIX 权限为 600），目录可通过 `ZPP_ABLESCI_STATE_DIR` 指定。等待期限由 `ZPP_ABLESCI_GIVE_UP_SECONDS` 控制，默认 180 秒。超时会尝试关闭求助，退款情况须在服务端核实。Run only one AbleSci process per account at a time. After an interrupted publication, check open requests and the account balance before retrying.

插件及核心离线测试可在安装测试依赖后运行。Run `python -m pytest tests -q` from the repository root.
