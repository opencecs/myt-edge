# Chromium 视频硬解验收工具

此目录只提供开发验证工具，不进入系统运行服务，不改变 Chromium 产品启动项、系统 keyring 或登录策略。工具只使用 Python 3.11+ 标准库。

验证链路：独立测试 Chromium → 本机 HTTP 媒体 → CDP Media 解码器记录 → 视频帧回调与截图 → GPU 信息及 CPU 采样。

## 准备

将以下两个文件放到测试设备的同一目录，例如 `/data/y4-browser-validation/`：

- `browser_acceptance.py`
- `player.html`

媒体由测试者提前准备，并记录编码器、分辨率、帧率及来源。默认矩阵使用以下文件，建议每个文件为 12 秒、1080p60：

| 文件 | 用途 |
|---|---|
| `h264.mp4` | H.264 HTML5 文件播放 |
| `vp9.webm` | VP9 HTML5 文件播放 |
| `vp8.webm` | VP8 HTML5 文件播放 |
| `h264-mse.mp4` | H.264 fragmented MP4，经 MediaSource 分块 append |

MSE 文件需要有效的初始化段和分片；工具从首 MiB 内的 `avcC` 读取 profile、兼容标志和 level。只有已确认媒体参数时才用 `--mse-codec avc1.XXXXXX` 覆盖。

确保桌面会话已就绪，设备当前允许播放验证。默认会话为 `/run/user/0/wayland-0`，D-Bus 为 `/run/user/0/bus`；其他会话通过 `--runtime-dir`、`--wayland-display` 指定。

## 运行

先用单个 H.264 做短测，输出目录必须尚不存在：

```sh
env -u LD_LIBRARY_PATH -u LD_PRELOAD -u Y4_MVX_POLL_REARM \
    python3 -B /data/y4-browser-validation/browser_acceptance.py \
    --media-dir /data/y4-browser-validation/media \
    --output /data/y4-browser-validation/results/h264-001 \
    --mode both --cases h264 --timeout 20
```

正式验收默认通过 `/usr/bin/chromium` 读取产品配置 `/etc/chromium.d/99-y4`，不在命令中重复 GPU 和解码特性参数，避免配置漂移。上例只清理当前测试进程继承的库路径、预加载库和旧实验开关，不修改调用者或系统环境。

随后使用新的输出目录，将 `--cases` 改为 `h264,vp9,vp8,h264-mse`，执行完整矩阵。`--mode hardware` 或 `--mode software` 可单独运行。默认每步超时为 35 秒，上例限制为 20 秒。

每种模式会新建空 profile，每个编码新建页面。软件对照只添加 `--disable-accelerated-video-decode`，保留产品 GPU 显示参数。仅在定位问题时使用 `--browser-arg`、`--hardware-arg`、`--software-arg` 传入临时参数；报告保留实际参数，不应把临时参数验证与产品默认配置验收混同。

`--password-store=basic` 仅用于工具创建的空测试 profile，防止首次启动被桌面 keyring 解锁提示阻塞；不要把这一参数复制到用户日常浏览器配置。工具拒绝已有 profile，不连接已有 Chromium。root 测试自动添加 `--no-sandbox`；这不代表普通用户和沙箱兼容性已经验证。

## 检查内容与结果

每个用例会等待实际 `requestVideoFrameCallback` 输出，再执行暂停、拖动到中间、继续、进入/退出全屏、播放结束，并保存截图。媒体错误和超时会保留现场；浏览器清理只针对本次新建的进程组。

HTTP 只监听 `127.0.0.1:8765`，CDP 只监听 `127.0.0.1:9223`。端口可改，已占用的 CDP 端口会被拒绝。HTTP 自检必须通过；测试浏览器使用 `--no-proxy-server`，工具自己的 loopback 请求也不经过代理。这些都是本机测试设置。

连接后会通过 `Browser.getBrowserCommandLine` 再次核对 profile 归属，避免误接其他浏览器。运行中检查 boot ID；启动周期变化会中止。每次验证使用独立输出目录，不混用不同启动周期的证据。

| 输出 | 内容 |
|---|---|
| `report.json` | boot ID、浏览器实际版本/参数、媒体 SHA256、GPU 信息、完整结果 |
| `hardware/`、`software/` | 两种模式各自的日志、profile 和用例结果 |
| `cdp-events.jsonl` | Media、Network、页面事件 |
| `cdp-commands.jsonl` | CDP 方法、参数、返回/超时时序 |
| `<编码>-samples.jsonl` | 播放状态、视频帧回调、CPU 与 RSS 采样 |
| `<编码>-result.json` | 解码器属性、错误、功能检查结果 |
| `<编码>-*.png` | 播放中、全屏、结束或失败现场截图 |

判定要求：

- 硬解必须同时具有明确的 V4L2 视频解码器名称和视频帧回调。`MojoVideoDecoder` 包装层名称、能力开关或低 CPU 单独都不足以确认。
- GPU 检查要求页面 WebGL 渲染器为 Mali，且浏览器 GPU compositing 为 enabled。截图仍需人工确认颜色、画面和黑屏问题。
- 软件对照要求明确的软件视频解码器名称；不能仅根据启动参数推断回退已生效。
- CPU 按单核百分比记录，多进程总和可以超过 100%。浏览器与 GNOME/Xwayland 分开统计；采样间启动又退出的短进程可能漏计。RSS 未扣除共享页。
- 所有机器活动都会进入系统 CPU 统计，不应把系统总量全部归因于视频。

退出码：

| 退出码 | 含义 |
|---|---|
| `0` | 所选矩阵的功能检查与解码器/GPU 证据全部通过 |
| `1` | 工具、启动或传输异常 |
| `2` | 播放失败或矩阵未完成 |
| `3` | 播放正常，但解码器/GPU 证据仍不足 |

## 验收边界

通过此工具不等于全部产品验收完成：

- 虚拟显示通过不等于实体 DP 屏幕通过；必须分别记录。
- 播放处于静音状态，不测量音画同步。
- MSE 使用一个 fragmented MP4 分块 append，不验证网站自适应码率切换、DRM 或业务站点兼容性。
- 帧回调和解码器名称不证明 DMA-BUF 零拷贝。
- 不覆盖 HEVC、AV1、10 位、HDR 或长期稳定性。
- root 下通过不等于普通用户、设备权限及沙箱通过。
- GPU/VPU 内核错误、AER 和系统稳定性需结合对应启动周期的外部日志核查。

## 离线工具测试

在编译服务器的 tools 仓库根目录运行：

```sh
python3 -B scripts/validation/browser_hwaccel/test_browser_acceptance.py
```

测试只使用临时目录、内存数据、socketpair 和本机随机端口，不启动 Chromium，不连接设备、不安装软件。覆盖 WebSocket 握手/掩码/分片/ping/大消息、超时缓冲、CDP 事件与 boot ID、CPU 进程标识、解码证据、HTTP Range/路径限制以及空 profile/basic 参数范围。

官方协议参考：[Media](https://chromedevtools.github.io/devtools-protocol/tot/Media/)、[SystemInfo](https://chromedevtools.github.io/devtools-protocol/tot/SystemInfo/)、[Runtime.evaluate](https://chromedevtools.github.io/devtools-protocol/tot/Runtime/#method-evaluate)。
