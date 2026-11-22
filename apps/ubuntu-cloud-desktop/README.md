# Y4 / Y12 Ubuntu 云桌面

一个 Ubuntu 24.04 ARM64 镜像可启动多个独立 Wayland 桌面。Sway 提供无显示器桌面，wayvnc 提供远程键鼠和画面，Foot 与 Thunar 提供终端和文件管理。

```sh
# 构建、导出与导入
docker buildx build --platform linux/arm64 --load -t myt/ubuntu-cloud-desktop:24.04 .
docker save myt/ubuntu-cloud-desktop:24.04 | gzip -1 > ubuntu-cloud-desktop-arm64.tar.gz
docker load -i ubuntu-cloud-desktop-arm64.tar.gz

# 在板上用有 Docker 权限的管理员执行
sudo bash desktop-up alice
sudo bash desktop-up bob

# 在客户端运行，替换 SSH 用户名和板地址
ssh -N -L 15901:/data/ubuntu-cloud-desktop/alice/connection/vnc.sock -L 15902:/data/ubuntu-cloud-desktop/bob/connection/vnc.sock ADMIN@BOARD
```

使用支持 VeNCrypt X509Plain 的客户端，例如 TigerVNC。地址分别为 `127.0.0.1::15901` 和 `127.0.0.1::15902`，用户名为 `desktop`。管理员将 `/data/ubuntu-cloud-desktop/<实例>/password` 单独发给相应用户。VNC 使用启动时生成的自签证书，SSH 隧道验证板端身份。每个实例只监听自己的 Unix 套接字。SSH 使用具备相应套接字访问权限的账号。超级键加回车打开终端，超级键加 E 打开文件管理器。

每个实例有自己的持久化 Home、容器进程、Wayland 套接字、凭证和 Docker 网桥。容器以非 root 启动，根层只读，无额外 capabilities，也不挂载 NAS 共享目录或 Docker socket。实例之间及与板上其他服务的网络访问应按部署现场防火墙策略限制。GPU 和 VPU 驱动、宿主内核及板上管理员仍处于共同信任边界。面向互不信任的租户时，需要独立虚拟机或设备，并验证硬件设备的分配和隔离。

## 硬件验证入口

```sh
# 先在宿主机核对节点归属与驱动
ls -l /dev/dri /dev/mali*
v4l2-ctl --list-devices

# 启动实验实例。指定明确对应 CIX 的 DRM render 节点。
sudo env GPU_RENDER_NODE=/dev/dri/renderD128 DESKTOP_VPU=1 \
  bash desktop-up gpu-test cix

docker exec ubuntu-desktop-gpu-test cix-run eglinfo -B
docker exec ubuntu-desktop-gpu-test ffmpeg -hide_banner \
  -f lavfi -i testsrc2=size=1280x720:rate=30 -frames:v 60 \
  -pix_fmt nv12 -c:v h264_v4l2m2m -num_capture_buffers 32 -y /tmp/vpu-test.h264
docker exec ubuntu-desktop-gpu-test ffmpeg -hide_banner \
  -c:v h264_v4l2m2m -num_capture_buffers 32 -i /tmp/vpu-test.h264 -f null -
```

CIX 模式使用板上 GPU 用户库，默认路径 `/opt/cixgpu-pro`，也可通过 `CIX_DESKTOP_LIB_DIR` 指定兼容库目录。使用 cix-run 启动支持 EGL/GLES 的应用，离屏 EGL 检查的 renderer 应为 Mali。默认使用软件合成。下文的 Mali 合成模式通过映射系统 DMA heap 支持 GLES2。仅看到设备节点或 FFmpeg 列出编解码器不算通过。可单独设置 `DESKTOP_VPU=1` 配合默认桌面模式验证应用编解码。应用 FFmpeg 编解码可独立使用，下文给出远程画面的 MVX 编码入口。

移除容器后用原名称启动可恢复原有 Home 与登录凭证。删除同名 Docker 卷会删除该桌面的数据。

## MVX 远程画面与 Mali 桌面合成

先导入新版镜像。下例在已验证的 Y12 上启动两个桌面。Y4 上使用自己的 DRM render 节点。

```sh
docker load -i ubuntu-cloud-desktop-mvx-arm64.tar.gz
sudo env DESKTOP_IMAGE=myt/ubuntu-cloud-desktop:mvx-stream DESKTOP_VPU=1 \
  DESKTOP_COMPOSITOR=mali GPU_RENDER_NODE=/dev/dri/renderD128 bash desktop-up alice cix
sudo env DESKTOP_IMAGE=myt/ubuntu-cloud-desktop:mvx-stream DESKTOP_VPU=1 \
  DESKTOP_COMPOSITOR=mali GPU_RENDER_NODE=/dev/dri/renderD128 bash desktop-up bob cix
```

`DESKTOP_COMPOSITOR=mali` 将 Sway 的合成切换到 GLES2，并只加映射 `/dev/dma_heap/system`。发生驱动初始化错误时可删除容器，再用 `DESKTOP_COMPOSITOR=pixman` 重启同名实例，数据卷保留。两种合成方式都可使用 MVX 编码。采集从 Wayland 读取画面，由 CPU 转 NV12，再交给 MVX 编 H.264。

```sh
# 在客户端连接；用有权访问相应套接字的 SSH 用户。
ssh -N \
  -L 15901:/data/ubuntu-cloud-desktop/alice/connection/vnc.sock \
  -L 16901:/data/ubuntu-cloud-desktop/alice/connection/h264.sock \
  -L 15902:/data/ubuntu-cloud-desktop/bob/connection/vnc.sock \
  -L 16902:/data/ubuntu-cloud-desktop/bob/connection/h264.sock ADMIN@BOARD

# 分别在两个客户端窗口打开
ffplay -flags low_delay -framedrop -probesize 32768 -analyzeduration 100000 -f mpegts tcp://127.0.0.1:16901
ffplay -flags low_delay -framedrop -probesize 32768 -analyzeduration 100000 -f mpegts tcp://127.0.0.1:16902
```

每个实例允许一路 H.264 观看连接，断开后释放编码器。键鼠仍通过同实例的 TigerVNC 连接操作，画面可由独立的 H.264 播放器观看。两个套接字都通过 SSH 转发。播放连接不会接入另一份桌面数据。视频流当前不含音频。
