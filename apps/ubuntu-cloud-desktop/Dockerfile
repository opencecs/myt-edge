FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    sway wayvnc foot thunar dbus fonts-dejavu mesa-utils ffmpeg v4l-utils openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10000 -s /bin/bash desktop
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends wf-recorder python3 && rm -rf /var/lib/apt/lists/*
COPY desktop-stream /usr/local/bin/desktop-stream
COPY entrypoint /usr/local/bin/cloud-desktop
COPY sway.conf /etc/cloud-desktop/sway.conf
COPY cix-run /usr/local/bin/cix-run
RUN chmod 0755 /usr/local/bin/cloud-desktop /usr/local/bin/cix-run /usr/local/bin/desktop-stream
USER 10000:10000
ENV HOME=/home/desktop LANG=C.UTF-8
ENTRYPOINT ["/usr/local/bin/cloud-desktop"]
