# Y4 / Y12 板卡管理 API（y4-boardd）

版本：2026-11-12-r3（随 r191 固件交付；r3=全量 API 测试 49/49 后回填实测样例、修正 405 语义、补 GET /api/v1/mcu/firmware、fans 门禁实测细节）　来源：与 `board/daemon/{api,status,update,ota,monitor}.c` 及 `board/daemon/vendor/{fan_policy,fan1_tach}.c`、MCU `include/protocol.h`（0.1.39）逐字段核对，样例为 r191 实板抓取
当前板型风扇接口见第 6 节。r191 字段快照、错误码及实测样例保留其原始版本含义；新客户端先从 /api/v1/version 读取 device，再选择对应结构。

## 1. 传输与访问

| 项 | 值 |
|---|---|
| 监听 | **Unix 流套接字 `/run/y4-board/api.sock`**，0600，仅 root 可连（即鉴权边界） |
| 协议 | HTTP/1.1；响应恒为 `application/json`；`Connection: close`；`Cache-Control: no-store` |
| 远程访问 | `ssh root@<板IP> -L 18080:/run/y4-board/api.sock -N` 后访问 `http://127.0.0.1:18080` |
| 大文件上传 | >2 GB 不要用 macOS 系统 curl（off_t 限制）；用 Python `http.client` 流式发送 |

## 2. 通用约定

**成功响应**：200（查询/确认完成）、202（大上传已受理，处理异步进行）。所有响应体含 `"schema":1`。

**错误响应**（全部路由统一）：

```json
{"status": <负errno>, "error": "<机器可读码>"}
```

**HTTP 状态映射**（api.c `reason()` 全集）：

| HTTP | 含义 | 典型触发 |
|---|---|---|
| 200 OK | 请求完成 | GET 全部、confirm 成功 |
| 202 Accepted | 上传受理，异步处理 | update / mcu/firmware 收完 |
| 400 | 参数错 | Content-Length 非法、意外请求体 |
| 404 | 未知路由 | — |
| 405 | 方法不允许 | 方法既非 GET 也非 POST（实测：GET/POST 互打到对方路由返回 404 unknown_route，因为该路由对另一方法不存在） |
| 408 | 请求超时 | 上传超时（见各端点时限） |
| 409 | 冲突 | 已有任务在跑（-EBUSY） |
| 411 | 缺 Content-Length | POST 未带长度 |
| 413 | 载荷超限 | 超包上限 |
| 417 | Expect 失败 | 客户端发 `Expect: 100-continue`（请禁用） |
| 500 | 内部错误 | 响应构造溢出等 |
| 503 | 服务未就绪 | 首启初始化未完成 / 锁被占 / 任务态异常 |
| 507 | 存储不足 | /data 或 /var/lib 空间不足（-ENOSPC） |

## 3. 路由总表

| # | 方法 | 路径 | 请求体 | 用途 |
|---|---|---|---|---|
| 1 | GET | `/api/v1/hardware` | 无 | 全量硬件快照 |
| 2 | GET | `/api/v1/version` | 无 | 版本 + 槽位 + 一致性（轻量） |
| 3 | GET | `/api/v1/fans` | 无 | Y4 的 MCU 风扇策略与转速，或 Y12 的两个 SoC PWM 组与六路测速 |
| 4 | POST | `/api/v1/fans` | JSON（≤512 B） | Y4 写入 MCU 三路策略。Y12 写指定 SoC 风扇组并读回 PWM |
| 5 | GET | `/api/v1/temperature` | 无 | 三路温度轻量快照 |
| 6 | GET | `/api/v1/update/status` | 无 | rootfs/内核 OTA 状态机 |
| 7 | POST | `/api/v1/update` | .y4pkg（流式，Content-Length 必填） | 上传 OTA 包并启动应用 |
| 8 | POST | `/api/v1/update/confirm` | 必须为空 | 手动确认当前槽 + 触发内核 GC |
| 9 | GET | `/api/v1/mcu/update` | 无 | MCU 固件烧写任务状态 |
| 10 | GET | `/api/v1/mcu/firmware` | 无 | 当前生效的 MCU 签名固件包记录（active.json） |
| 11 | POST | `/api/v1/mcu/firmware` | MCU 签名固件包（≤1,049,736 B） | 上传并烧写 MCU 固件 |
| 12 | POST | `/api/v1/mcu/cancel` | 必须为空 | 请求取消 MCU 烧写 |

## 4. GET /api/v1/hardware —— 字段字典

顶层（status.c `y4_board_sample_json`）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema` | int | 恒 1 |
| `device` | string | **设备族，编译期常量**：`"Y4"`（本固件）；Y12 构建翻转 Makefile `DEVICE` 开关即得 `"Y12"`，程序内无运行时推断 |
| `sampled_ms` | u64 | 采样时刻（守护进程时钟，ms） |
| `age_ms` | u64 | 快照年龄 = now − sampled_ms |
| `fresh` | bool | 采样是否新鲜（窗口 5000 ms 内且 magic 正确） |
| `maintenance` | bool | 维护模式标记（仅在 fresh 时可信） |

### versions（/etc/y4-release.json 原文内嵌 + uname）

| 字段 | 类型 | 含义 |
|---|---|---|
| `rootfs` | string | rootfs 交付版本 `YYYYMMDD-rNNN`（OTA 单调性判据） |
| `build_commit` | string | 构建源提交（12 位） |
| `built_at` | string | ISO 构建时间 |
| `kernel` | string | 镜像声明的内核 release |
| `kernel_image_sha256_16` | string | 内核 Image 摘要前 16 hex（板上身份自助核对） |
| `fw_gpu` | obj | `{file:"mali_csffw.bin", sha256_16}` GPU 固件指纹 |
| `fw_vpu` | obj | `{fwb_count}` VPU 固件分片数 |
| `ota_package_schema_max` | int | 代理支持的 OTA 包格式（2 = rootfs+内核原子对） |
| `kernel_running` | string | 实际 `uname -r`；与 `kernel` 应一致 |

### mcu

| 字段 | 类型 | 含义 |
|---|---|---|
| `status` | int | 0 正常；负 errno（采样失效为 -ESTALE/-11） |
| `firmware` | string | `"maj.min.patch"`；无固件/不新鲜为 `""`（0.1.38 中 stored 0 是合法主版本，勿当空） |
| `uid_valid` | bool | MCU UID 是否可信读取 |
| `uid` | string | 24 hex（12 字节）；产线 UID 流程的身份锚 |
| `fan_status` | int | 风扇采样错误码（0 正常） |
| `fault_bits` | u32 | MCU 故障位图 |

### temperatures（CPU / inside / pcie_switch 同构）

| 字段 | 类型 | 含义 |
|---|---|---|
| `valid` | bool | 本路温度有效 |
| `status` | int | 采集错误码 |
| `millicelsius` | int | 毫摄氏度 |
| `source_age_ms` | int | 该路源数据年龄（各路独立过期） |

### cooling

| 字段 | 类型 | 含义 |
|---|---|---|
| `status` | int | 冷却采样错误码 |
| `requested_pwm` | u32 | 主机侧冷却请求百分比。Y4 V11 三路当前实际占空比读取各自的 `effective_pwm` |
| `FAN1_owner` | string | 恒 `"linux"`（FAN1 归内核 pwm-fan 驱动自动热控；守护进程**只读不写**） |
| `FAN1` | obj | `{valid, status, rpm}` —— FAN1 实时转速（/sys/class/hwmon 中 `pwm-fan` 芯片的 `fan1_input`，零 exec 直读） |
| `MCU_fans[]` | array | 4 项（connector=`FAN3`..`FAN6`）：`{connector, valid, monitored, rpm, effective_pwm}`；`monitored`=该口接了风扇（tach 检出）；`effective_pwm` 为 percent 0-100 |
| `policy` | obj | **风扇策略**（MCU ≥0.1.39；字段见 §6 GET /api/v1/fans）。`error=-EOPNOTSUPP` 表示 MCU 固件 <0.1.39，其余字段无证据 |

### policy（cooling 内嵌对象，MCU 0.1.39 起）

| 字段 | 类型 | 含义 |
|---|---|---|
| `mode` | string | `legacy`（出厂默认，行为与 0.1.38 完全一致）/ `auto` / `manual`；MCU 不可读时 `unknown` |
| `status` | int | 持久化状态：0 无记录 / 1 写入中 / 2 已提交（或开机读到有效记录）/ 3 上次写入失败（策略在 RAM 生效但**未持久化**） |
| `stored` | [u8×3] | FAN3/4/5 已存占空比（percent 0-100，MCU flash 永久保存） |
| `host_online` | bool | 主机心跳是否在线（MCU 口径：10 s 内有心跳且开机后见过至少一次） |
| `error` | int | 策略读回错误码（0 正常；-95/-EOPNOTSUPP=MCU 固件过旧） |

### health（MCU 健康）

`{status, reset_flags(复位来源位图), fault_count(累计故障), communication_errors(通信错计数), minimum_stack_pointer(最小栈余量)}`，status 为采样错误码。

### backends（MCU 后端总线）

`{status, pwm_hz(PWM 频率), spi_hz(SPI 频率), dma_completed(DMA 完成计数), error_events(错误事件计数)}`。

### slots（盘槽）

| 字段 | 类型 | 含义 |
|---|---|---|
| `status` | int | 采样错误码 |
| `online_mask` | u32 | 位图：bit n = 实体槽 n 在线 |
| `activity_status` | int | 活动采样错误码 |
| `activity_mask` | u32 | 位图：bit n = 槽 n 有 IO 活动 |
| `type[]` | u32×4 | 每槽类型编码 |

### network

| 字段 | 类型 | 含义 |
|---|---|---|
| `status` / `address_status` / `route_status` | int | 网络/地址/路由三路独立错误码 |
| `default_egress` | u32 | 默认出口 ifindex |
| `physical[]` | 3 项 | role=`LAN_2G5`/`SFP1_10G`/`SFP2_10G`：`{role, valid, present(在位), carrier(载波), status, ifindex, speed_mbps(实时，内核 /sys/class/net/*/speed 口径，零 exec), mac, rx_bytes, tx_bytes, rx_packets, tx_packets, counters_error}`——计数器为内核统计自开机累计（每秒采样一次直读 `/sys/class/net/*/statistics/`，无占用；`counters_error!=0` 时四个计数不可信） |
| `bond` | obj | `{mode, state, state_status, ad_info_valid, partner_present, aggregator_id, aggregator_ports, partner_key, active_mask, lacp_mask}`（真实 LACP 状态来自内核采集，非载波计数） |
| `logical[]` | 2 项 | role=`LAN_2G5`/`BOND_10G`：`{role, link_valid, carrier, address_valid, address_status, ipv4, prefix_length, gateway, manager_state_known, manager_state, rx_bytes, tx_bytes, rx_packets, tx_packets, counters_error}`（NetworkManager 状态 + 内核计数；`BOND_10G` 的计数=bond10g 聚合总量） |

### display_status / initialization

| 字段 | 类型 | 含义 |
|---|---|---|
| `display_status` | int | 屏幕适配器错误码（0 正常） |
| `initialization` | obj | 首启初始化：`{valid, receipt_status, attempt, max_attempts(恒5), status, completed, network_safe, phase}`——**POST 上传端点的准入门**：completed=false 时上传被 503 拒 |

## 5. GET /api/v1/version —— 字段字典

轻量版（update.c `y4_board_version_json`），实板样例：

```json
{"schema":1,"device":"Y4", ...versions 同上...,
 "kernel_running":"6.6.89-cix-build-generic","mcu":"0.1.39",
 "slot":{"selected":"B","boot_ok":true,"boot_count":0,"boot_count_remaining":3,
   "boot_kernel":null,"boot_kernel_a":"20261112-r189","boot_kernel_b":"20261112-r191",
   "running":{"partuuid":"0cbed5a4-41bd-5346-861d-35430ee2f4eb","slot":"B"}},
 "consistency":{"kernel_sync":true,"slot_confirmed":true,"manifest_readable":true},
 "slot_error":0}
```
（r191 实板抓取：OTA 后 B 槽运行、A/B 各绑 r189/r191 内核目录）

| 字段 | 含义 |
|---|---|
| `device` | 编译期设备族常量（`"Y4"`；Y12 构建为 `"Y12"`） |
| `slot.selected` | grubenv `default` 指向的槽（下次启动） |
| `slot.boot_ok` | 当前启动是否已健康确认 |
| `slot.boot_count` | 未确认启动计数 |
| `slot.boot_count_remaining` | 距 3 次计数自动回滚的剩余次数（已达 3 次为 0） |
| `slot.boot_kernel` | legacy 单键（旧布局板才有；新布局为 null） |
| `slot.boot_kernel_a` / `boot_kernel_b` | **每槽绑定的内核目录**（boot FAT `/kernels/<交付版本>/`，OTA 实装后核心字段） |
| `slot.running` | `{partuuid, slot}`：当前运行槽的 PARTUUID 与字母 |
| `consistency.kernel_sync` | `kernel_running` == 镜像声明 `kernel`（内核与 rootfs 配对一致性） |
| `consistency.slot_confirmed` | 同 `slot.boot_ok` |
| `consistency.manifest_readable` | /etc/y4-release.json 可读 |
| `slot_error` | grubenv 读取错误码（0 正常；-2 格式错、-11 缺文件） |

## 6. 双板风扇接口

下表对应当前 `board/daemon/status.c` 的 GET 和 `board/daemon/api.c` 的 POST 实现。

| 板型 | GET /api/v1/fans | POST /api/v1/fans |
| --- | --- | --- |
| Y4 V11 | 按 `MCU_fans[].connector` 取 FAN3、FAN4、FAN5，依次显示为风扇 1、2、3。原始数组保留 FAN6 作协议兼容。SoC 的 `FAN1` 是单独的只读测速。 | 手动请求使用 `policy=manual` 及 `fan3/fan4/fan5` 百分比，三路随 MCU 策略持久化。 |
| Y12 | `groups[]` 返回组 ID 和 `pwm_raw`（0–255），`fans[]` 按 FAN1–6 返回 `id/group/rpm`。组 PWM 或转速读取失败时相应值为 `null`。 | 例如 `{"group":"pwm-fan-out0","percent":20}`。0–100 的占空比写入该组的 hwmon 并读回，成功后返回两组与六路测速。 |

Y12 的 `pwm-fan-out0` 对应 PWM3，一起驱动 FAN2、FAN3、FAN5；`pwm-fan-out2` 对应 PWM5，一起驱动 FAN1、FAN4、FAN6。六个插座各有独立测速。两组在 UEFI 和 Fastboot 默认 20%，Linux 初始为 20%，按组手动调节。
Y4 物理 FAN1 在 UEFI 保持 50%。Linux 正常监控时，SoC 温度低于 45°C 使用 30%，45°C 至低于 70°C 使用 50%，达到 70°C 使用 100%。内核启动保护和温度或测速失效时采用保护占空比 100%。FAN2 在 Y4 设备树中禁用。

### Y4 V11 的 MCU 策略

MCU 独立控制 FAN3、FAN4、FAN5，先输出默认 20% PWM，再接通 12 V。三路的模式如下。

| 模式 | 主机在线 | 主机失联（10 s 无心跳，含重启/OTA 期间） | 持久化 |
|---|---|---|---|
| `auto` | 三路固定 **20%** | **20%**（V11 MCU 0.1.43 起） | 模式存 MCU flash |
| `manual` | 三路=已存占空比（`fan3/fan4/fan5`，percent 0-100，0=停转） | **20%**（V11 MCU 0.1.43 起） | 模式+三路占空比存 MCU flash，断电/重启后自动恢复 |
| `legacy` | 三路固定 20% | 20% | 模式存 MCU flash |

- **出厂/无记录 = legacy**：V11 从 20% 启动。
- 失联判定：MCU 10 s 收不到心跳（`fault_bits` 的 bit4 `FAULT_HOST`），与 `host_online` 字段同口径。
- 守护进程退出期间由 MCU 维持风扇输出。
- 手动 `stored` 值 0 = 三路里对应风扇停转；主机失联后回到 20%。
- 持久化写入 MCU 内部 flash（追加日志+CRC32C，掉电安全）；`status=3` 表示上次写入失败（RAM 内已生效但未持久化），重试 POST 即可。
- POST 是同步的：MCU flash 写入期间 I2C 会被阻塞，守护进程轮询至多 ~12 s；期间其他 MCU 采样可能短暂失败一两个周期（快照 `fresh` 窗口 5 s 内自愈）。

V11 自 MCU 0.1.43 起只配置 FAN3–FAN5。MCU 先建立 25 kHz、20% PWM，再使能 12 V。默认及 `auto`、`legacy` 模式固定 20%。显式 `manual` 请求可调节三路占空比，主机心跳超时回到 20%。温度仍用于显示，堵转仍记录，二者均不触发自动提速。

### Y4 POST /api/v1/fans

| 项 | 值 |
|---|---|
| 请求体 | `{"policy":"auto"}` 或 `{"policy":"manual","fan3":30,"fan4":50,"fan5":80}`；也接受 `{"policy":"legacy"}`（回退） |
| 占空比单位 | **percent 0-100**（整数；与 `effective_pwm` 同口径，不是 0-255） |
| 校验 | manual 必须带 fan3/fan4/fan5 三键（缺/越界 400 `fans_duty_invalid`，实测负数同样 400）；auto/legacy 带 fan3-5 键也会 400（保持语义无歧义）；policy 值非法或 JSON 畸形 400 `fans_policy_invalid`；body>512 B 或 **Content-Length: 0** 413 `fans_body_too_large` |
| 门 | 首启初始化未完成 503（同上传端点）；缺 Content-Length 头 411；MCU <0.1.39 → 503 `fan_policy_unsupported`；mcu.lock 被占 >2 s → 503 `fans_busy`；MCU 写入失败/超时 → 503 `fans_apply_failed` |
| 成功 | 200 + 策略回读 JSON（见下）；**模式写入即生效**，`status=2` 表示已持久化 |

成功响应样例：

```json
{"schema":1,"device":"Y4","policy":{"mode":"manual","status":2,"stored":[30,50,80],"error":0}}
```

### Y4 GET /api/v1/fans

只读，不碰 MCU 总线（读守护进程最近一次采样）：

```json
{"schema":1,"device":"Y4","sampled_ms":...,"age_ms":0,"fresh":true,
 "policy":{"mode":"manual","status":2,"stored":[30,50,80],"host_online":true,"error":0},
 "FAN1":{"valid":true,"status":0,"rpm":1234},
 "MCU_fans":[{"connector":"FAN3","valid":true,"monitored":false,"rpm":0,"effective_pwm":30}, ...FAN4/FAN5/FAN6]}
```

- `policy`/`FAN1`/`MCU_fans` 字段语义同 §4；`MCU_fans[].effective_pwm` 是 MCU 实测施加值（percent），manual 模式下应≈stored 值。
- Y4 的 SoC FAN1 由内核驱动自动调速，板载 API 提供测速。
- r191 实板抓取（auto 模式）：

```json
{"schema":1,"device":"Y4","sampled_ms":195335,"age_ms":644,"fresh":true,
 "policy":{"mode":"auto","status":2,"stored":[30,50,80],"host_online":true,"error":0},
 "FAN1":{"valid":true,"status":0,"rpm":1699},
 "MCU_fans":[{"connector":"FAN3","valid":true,"monitored":false,"rpm":0,"effective_pwm":100},
             {"connector":"FAN4","valid":true,"monitored":true,"rpm":9120,"effective_pwm":100},
             {"connector":"FAN5","valid":true,"monitored":true,"rpm":0,"effective_pwm":100},
             {"connector":"FAN6","valid":true,"monitored":false,"rpm":0,"effective_pwm":100}]}
```
（FAN4/FAN5 effective 恒 100 是本板 FAN5 转速线读 0 触发的堵转全局保护，见 §11；FAN1 1699 rpm 为只读实测）

## 7. GET /api/v1/temperature —— 温度轻量端点

三路温度的独立小快照（结构与 hardware 的 `temperatures` 完全一致，方便应用单独轮询）：

```json
{"schema":1,"device":"Y4","sampled_ms":...,"age_ms":0,"fresh":true,
 "temperatures":{"CPU":{...},"inside":{...},"pcie_switch":{...}}}
```

来源全部零 exec：CPU=SCMI hwmon（内核）；inside=板载 TMP75（I2C ioctl）；pcie_switch=PCIe 交换芯片（专用采集子进程缓存，带 `source_age_ms`）。

## 8. OTA 接口

### POST /api/v1/update

| 项 | 值 |
|---|---|
| 请求体 | `.y4pkg`（Ed25519 签名包；schema 1=仅 rootfs / schema 2=rootfs+内核原子对） |
| 头 | `Content-Length` 必填（411 缺失）；禁用 `Expect: 100-continue`（417） |
| 大小上限 | 24 GiB（超出 413） |
| 空间门槛 | `/data` 空闲 ≥ 包大小 + 256 MiB（不足 507） |
| 接收时限 | 1 小时（超时 408 并丢弃未完成上传） |
| 并发 | 同一时刻仅一个 OTA 任务（忙时 409） |
| 首启门 | 首启初始化未完成（initialization.completed=false）时 503 |
| 成功 | `202` + 状态 JSON；之后轮询 `GET /api/v1/update/status` |

包内格式见 `docs/kernel-ota-design.md`；旧布局板收 schema 2 会在 manifest 阶段被拒（不写任何字节）。

### GET /api/v1/update/status —— 字段字典

```json
{"schema":1,"job":"2f1d6e08...","phase":"idle","status":0,
 "bytes_written":0,"image_bytes":0,"version":"","slot":"","busy":false,
 "payload_sha":"","payload_hash":"","package_schema":0,
 "kernel_release":"","kernel_bytes_written":0,"kernel_bytes":0}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `job` | string | 32 hex 任务 ID（= 暂存文件名） |
| `phase` | enum | 见下方状态机 |
| `status` | int | 0 进行中/空闲；负 errno = 失败原因（-74=-EBADMSG 哈希/签名不符、-28=-ENOSPC 空间不足、-22=-EINVAL 门禁拒绝、-5=-EIO IO 错误） |
| `bytes_written` / `image_bytes` | u64 | rootfs 写入进度 / sparse 逻辑总长 |
| `version` | string | 目标 rootfs 版本（manifest 声明） |
| `slot` | string | 目标槽（非活动槽）："A"/"B" |
| `busy` | bool | 任务存在且未终结（期间新 POST 409） |
| `payload_sha` / `payload_hash` | string | manifest 声明的 rootfs sha256 / 实测写入哈希（一致才进入内核阶段） |
| `package_schema` | int | 1 或 2 |
| `kernel_release` | string | schema 2 时内核 release |
| `kernel_bytes_written` / `kernel_bytes` | u64 | 内核载荷写入进度 / 总长（schema 1 恒 0） |

**phase 状态机**（schema 2 全路径；schema 1 从 verifying 后直达 grubenv）：

```
idle → accepted → open → verifying(验签) → manifest(门禁/旧布局拒) → version(单调性)
     → slot → device → writing(rootfs 写入) → hashing → verifying(rootfs 回读)
     → writing-kernel(N1/N2) → verifying-kernel(N3 UTS+哈希) → publishing-kernel(N4)
     → grubenv(N5/N6 每槽绑定切换) → ready → rebooting(有序重启请求已发)
```

失败语义（任意相位失败）：**grubenv 永不改动、目标槽旧内容不再触碰后续流程**，系统照常运行；`/kernels/<版本>.new-*` 暂存残留无害，由下次确认/OTA 清理。

### POST /api/v1/update/confirm

- 请求体必须为空（400 if any）
- 作用：当前运行槽 `boot_ok=1`、`boot_count=0`，刷新 `/data/y4-board/confirmed.json`，并触发内核目录 GC（无引用的 `/boot/efi/kernels/` 目录与 `.new-*` 残留全清）
- 成功 200 + 状态 JSON；grubenv 不可读时 503

## 9. MCU 固件接口

### GET /api/v1/mcu/firmware —— 当前生效包记录

- 无请求体；200 + `/var/lib/y4-board/active.json` 原文（经 **POST /api/v1/mcu/firmware** 部署的包的收据：`{schema,package,sha256,job,version,uid,sequence}`）
- 状态目录无记录时返回 `{"schema":1,"active":false}` —— **这不是"MCU 没固件"**：镜像首启初始化刷入内置包（mcu-builtin.json）不写 active.json；**MCU 实际运行版本一律以 hardware 接口的 `mcu.firmware` 为准**（实测：本板首启自动刷 0.1.39 后此端点仍 active:false，mcu.firmware=0.1.39）

### POST /api/v1/mcu/firmware

| 项 | 值 |
|---|---|
| 请求体 | 签名 MCU 固件包 |
| 大小上限 | **1,049,736 字节**（超出 413） |
| 时限 | 120 秒 |
| 并发 | 固件任务与 OTA 独立；但 MCU 任务互斥（忙 409） |
| 空间 | /var/lib 状态目录需 ≥ 包大小 + 4 MiB；历史 firmware-*.bin ≥32 个时 507 |
| 首启门 | 同 update（503） |

### GET /api/v1/mcu/update —— 字段字典

| 字段 | 类型 | 含义 |
|---|---|---|
| `job` | string | 任务 ID（32 hex） |
| `phase` | enum | 任务主相位（idle 等） |
| `status` | int | 任务错误码（0 正常） |
| `verified` | bool | 固件包验签通过 |
| `sequence_valid` | bool | 同 verified（防降级序列号校验结果） |
| `running` | bool | 烧写助手进程在跑 |
| `overdue` | bool | 任务超时未完成 |
| `cancel_requested` | bool | 已请求取消 |
| `blocked` | bool | 助手被阻塞（锁冲突等） |
| `helper_phase` | string | 助手子相位：writing/verifying/complete/unavailable/malformed |
| `helper_status` | int | 助手上报错误码（-61=-ENODATA 无数据） |
| `progress_valid` | bool | 进度计数可信（writing/verifying/complete 且计数自洽） |
| `bytes_written` / `bytes_verified` / `image_bytes` | u64 或 null | 写入/校验字节 / 镜像总长 |
| `version` | string | 目标固件版本 |
| `uid` | string | 目标 MCU UID |
| `sequence` | u64 | 固件序列号 |

### POST /api/v1/mcu/cancel

- 空请求体；置 `cancel_requested`，由任务循环收敛；无任务时无效（不报错）

## 10. 集成示例（Python，经 SSH 隧道）

```python
import http.client, os

def board_post(path, file_path):
    size = os.path.getsize(file_path)
    conn = http.client.HTTPConnection("127.0.0.1", 18080, timeout=1800)
    conn.putrequest("POST", path, skip_accept_encoding=True)
    conn.putheader("Content-Length", str(size))
    conn.endheaders()
    with open(file_path, "rb") as f:
        while block := f.read(4 * 1024 * 1024):
            conn.send(block)
    resp = conn.getresponse()
    return resp.status, resp.read().decode()

print(board_post("/api/v1/update", "ota-20261112-rNNN.y4pkg"))  # (202, status json)
```

轮询建议 ≥1 s；rootfs 写入+回读约 5-8 分钟，内核三相位在最后 ~1 秒内原子完成。

风扇策略（普通 HTTP 小请求，同样走隧道）：

```python
import http.client, json

def board_call(method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", 18080, timeout=30)
    payload = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=payload,
                 headers={"Content-Length": str(len(payload))} if payload else {})
    resp = conn.getresponse()
    return resp.status, resp.read().decode()

print(board_call("GET", "/api/v1/fans"))
# 手动 30/50/80（percent，永久保存）
print(board_call("POST", "/api/v1/fans", {"policy": "manual", "fan3": 30, "fan4": 50, "fan5": 80}))
# 自动：固定 20%
print(board_call("POST", "/api/v1/fans", {"policy": "auto"}))
print(board_call("GET", "/api/v1/temperature"))
```

## 11. 已知边界

- 无应用层鉴权：0600 套接字即边界；非 root 进程连不上（myt_desktop 为 root 服务可直连）
- OTA 上传期间只读端点不受影响；OTA 暂存占用 `/data`，成功即删、失败保留（下次任务清理）
- 上传中断（客户端断开）自动丢弃半成品文件，不留垃圾
- **风扇策略依赖 MCU 0.1.39**（随 r191 固件交付）：MCU 固件未升级时 GET/POST /api/v1/fans 返回 503 `fan_policy_unsupported`，hardware 里 `policy.error=-95`
- Y4 POST /api/v1/fans 执行期间（MCU flash 写入 ~1-4 s）MCU 的 I2C 会短暂阻塞：相邻 1-2 个采样周期可能出现 mcu 相关错误码，快照 5 s 窗口内自愈，风扇转速不受影响（硬件 PWM 自主运行）
- Y4 三路 MCU 风扇策略存 MCU flash，rootfs OTA / 刷机 / 断电都不丢；只有再次 POST 才会改变
- Y4 的 SoC FAN1 温控独立于板载服务。Y12 的两组机箱风扇可以通过本节所述组接口单独设置。
