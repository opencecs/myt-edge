# Y12 实机验证

固件 20261119-r292，Docker 27.3.1。ARM64 归档在 Windows 和板上核对 SHA256 一致，板上通过 docker load 导入。

- 两个 1920×1080 桌面分别完成 VNC/TLS 登录与帧读取，跨实例密码被拒绝，断开后可重连。
- A 中写入的文件对 B 不可见，A 重启后文件保留。
- 两实例各自用 Mali-G720-Immortalis 执行 EGL/GLES 离屏渲染，读回像素 RGBA 255 0 0 255。
- 两实例并发执行 720p H.264。每个实例经 MVX /dev/video1 硬编 60 帧，再经 /dev/video0 硬解 60 帧，使用 32 个 capture buffers。默认缓冲区曾在短测中丢帧。
- Sway 软件合成。Y12 上实验的 GLES2 合成模式在 GBM 初始化失败。远程 VNC 画面没有接入 MVX 硬编码。
- Y4 的 SSH 地址未连通。本次只完成 Y12 物理板验收。该测试未测长时间压力或最大并发数。

镜像 SHA256：07f6ea60b080ace3aafcb4bbcbb09f02c2eb0b7e76704242bfa0884ced8804d0。
