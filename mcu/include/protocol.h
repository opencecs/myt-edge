#ifndef Y4_MCU_PROTOCOL_H
#define Y4_MCU_PROTOCOL_H
#include <stdint.h>
#define Y4_COOL_DATA_LEN 7u
#define Y4_COOL_TIMEOUT_SECONDS 2u
enum { COOL_IDLE=0, COOL_PENDING=1, COOL_COMMITTED=2, COOL_REJECTED=3, COOL_EXPIRED=4 };
/* 0.1.26+: begin token(nonzero, different from last begin); 7 payload bytes
 * = FAN3..6 duty, board temperature LE i16 deci-C, flags exactly1; CRC8
 * poly07/init0 covers token+payload; commit same token last. Partial frames
 * retain previous control for at most two MCU seconds, then fail full.
 * Legacy valid=0 cancels staging immediately. Status is read-only. */
#define Y4_ADDR 0x42
#define Y4_PROTO 2
#define Y4_BOARD 4
#define Y4_FW_MAJOR 0
#define Y4_FW_MINOR 1
#define Y4_FW_PATCH 49
/* Y4 V11 duty percent, not RPM or a cross-product thermal policy. UI fans
 * 1/2/3 map to protocol FAN3/4/5; preserve reserved FAN6 wire fields.
 * Y12 firmware/host protocols require their own verified adapter. */
#define Y4_V11_FAN_DEFAULT 15u
/* 0.1.39: host-directed fan policy for FAN3/FAN4/FAN5 (registers 0x72/0x73/
 * 0x7c..0x7e; contract below next to R_FAN). Protocol2, the cooling frame,
 * the display streams and the frozen health/backend blocks are unchanged.
 * FAN6 keeps the legacy behavior in every mode. */
/* 0.1.31: factory auto-maintenance release; protocol2 and display stay unchanged. */
/* 0.1.28 independent IX8024 temperature frame. BEGIN nonzero and different
 * from last accepted BEGIN; DATA6={temp i16 deci-C,error i16,age u16 ms} LE.
 * CRC8 poly07/init0 covers BEGIN+DATA6. COMMIT is the same token, written last.
 * OUT6 is read-only {tempLE2,errorLE2,valid,status}. Status idle0/pending1/
 * committed2/rejected3/expired4. Valid temp range -400..1250; error -4095..0.
 * Successful values live <=15000ms-host_age-BEGIN/COMMIT_elapsed. Invalid error frames publish
 * temp=0, valid=0. Bad/partial frames never extend a previous value's life.
 * Partial frames expire in 2000ms. A stale but well-formed sample publishes
 * error=-ESTALE/status=expired. Snapshot sequence brackets every OUT change.
 * This channel never changes board/SoC temperature or fan controls. */
#define Y4_IX_TEMP_DATA_LEN 6u
#define Y4_IX_TEMP_LIFE_MS 15000u
#define Y4_IX_TEMP_PARTIAL_MS 2000u
enum { IX_TEMP_IDLE=0, IX_TEMP_PENDING=1, IX_TEMP_COMMITTED=2,
       IX_TEMP_REJECTED=3, IX_TEMP_EXPIRED=4 };
enum {
 R_IX_TEMP_OUT=0x0a, R_IX_TEMP_ERROR=0x0c, R_IX_TEMP_VALID=0x0e, R_IX_TEMP_STATUS=0x0f,
 R_IX_TEMP_BEGIN=0x18, R_IX_TEMP_DATA=0x19, R_IX_TEMP_CRC=0x1f, R_IX_TEMP_COMMIT=0x20,
 R_COOL_BEGIN=0x25, R_COOL_DATA=0x26, R_COOL_CRC=0x2d,
 R_COOL_COMMIT=0x2e, R_COOL_STATUS=0x2f,
 R_HEALTH=0x90, /* read-only boot diagnostic snapshot; see Y4_HEALTH_* */
 R_CHIP_ID=0x80, /* read-only: UID1 marker + 12-byte GD32 factory ID */
 R_SYSTEM=0x00, R_FAN=0x01, R_HOST_HB=0x05, R_ONLINE=0x09,
 R_RPM=0x10, R_IP=0x21,
 R_MAGIC=0x30, R_PROTOCOL=0x34, R_BOARD=0x35, R_VERSION=0x36,
 R_CAPS=0x39, R_UPTIME=0x3a, R_SNAPSHOT=0x3e, R_FAULT=0x3f,
 R_ACTIVITY=0x40, R_DISK_FAULT=0x41, R_LOCATE=0x42,
 R_BRIGHTNESS=0x43, R_SCREEN=0x44, R_PAGE=0x45,
 R_TEMP=0x46, R_TEMP_VALID=0x48, R_FAN_MASK=0x49,
 R_TACH_MASK=0x4a, R_HOST_TIMEOUT=0x4b, R_MIN_RPM=0x4c,
 R_NET_PORTS=0x54, R_NET_HB=0x5a, R_SOC_TEMP=0x5b, R_SOC_VALID=0x5d, R_SLOT_STATE=0x74, R_SLOT_TYPE=0x78, R_ALERT=0x50, R_RESET_REASON=0x51, R_PPR=0x52, R_EFFECTIVE=0x60, R_LED_GPIO=0x64, R_LCD_DIAG=0x68,
 R_FAN_POLICY_STATUS=0x72, R_FAN_POLICY_MODE=0x73, R_FAN_POLICY_DUTY=0x7c,
 R_BACKENDS=0xe4 /* read-only backend diagnostic block, exactly
  Y4_BACKENDS_LEN bytes; old health 0x90..0xe3 stays untouched */
};
/* R_FAN..R_FAN+3 are the REQUESTED duty percent per channel (FAN4/FAN5 are
 * the installed hardware fans); R_EFFECTIVE..+3 report the duty ACTUALLY
 * APPLIED per channel after safety/backend resolution and are read-only.
 * RPM values are real tachometer counts; duty is never derived from them
 * and RPM is never derived from duty. While FAULT_PWM is latched the
 * backend-owned channels are physically forced full and R_EFFECTIVE
 * reports the actual forced 100, never the requested value. */
/* 0.1.39 fan policy (FAN3/FAN4/FAN5 = channels 0..2 only; FAN6/channel 3
 * keeps the legacy cooling-frame behavior in every mode). All values are
 * duty PERCENT 0..100, matching R_FAN/R_EFFECTIVE semantics.
 *
 * R_FAN_POLICY_MODE (0x73, rw): 0=legacy 1=auto 2=manual; other values are
 *   ignored. A mode write is the COMMIT of a policy transaction: the host
 *   MUST write the three R_FAN_POLICY_DUTY registers first, then the mode.
 *   Accepting a mode write (1) switches the active policy immediately and
 *   (2) requests persistence of {mode,duty[3]} to the MCU's internal flash
 *   record log (last valid record wins; survives MCU and board power loss).
 *   Until persistence completes R_FAN_POLICY_STATUS reads PENDING.
 * R_FAN_POLICY_DUTY (0x7c..0x7e, rw): stored/manual duties for FAN3/4/5,
 *   0..100 percent; values above 100 are ignored. They take effect only
 *   through a mode commit (auto ignores them while the host is online).
 * R_FAN_POLICY_STATUS (0x72, read-only): 0=none/factory, 1=persisting,
 *   2=last persist committed (or a valid record was loaded at boot),
 *   3=last persist failed (fans still follow the in-RAM policy; the
 *   setting is NOT durable until a later write reports 2).
 *
 * V11: legacy and auto use the fixed default for FAN3–FAN5 (15% since
 * 0.1.48, 20% in 0.1.43–0.1.47). Manual uses the stored duty with a live
 * heartbeat. Host timeout and per-channel stall restore the default.
 * The V11 FAN6 output is unused. */
enum { Y4_FAN_POLICY_LEGACY=0, Y4_FAN_POLICY_AUTO=1, Y4_FAN_POLICY_MANUAL=2 };
enum { Y4_FAN_POLICY_NONE=0, Y4_FAN_POLICY_PENDING=1,
       Y4_FAN_POLICY_COMMITTED=2, Y4_FAN_POLICY_FAILED=3 };
enum { Y4_SLOT_NONE=0,Y4_SLOT_PCIE=1,Y4_SLOT_NVME=2,Y4_SLOT_RK182X=3,Y4_SLOT_M50=4 };
enum { CAP_FAN=1, CAP_TACH=2, CAP_LED=4, CAP_LCD=8, CAP_WATCHDOG=16, CAP_LCD_POWER=32, CAP_LCD_DIAG=64, CAP_SLOT_ID=128, CAP_LCD_NET=CAP_SLOT_ID };
/* R_FAULT is a full-byte mask (the host fan_fault_mask is the whole byte).
 * Bits 0..3 are per-channel tachometer stall faults. FAULT_HOST means the
 * host heartbeat timed out. FAULT_PWM means the optional hardware fan PWM
 * backend (enabled builds only) latched its fail-safe: the backend-owned
 * fan channels are physically forced full as GPIO and R_EFFECTIVE reports
 * the applied duty (100), never the requested duty, while latched. It is
 * sticky for the boot: no automatic reinitialization or retry exists. It
 * is never set merely because the optional backend is compiled out. */
enum { FAULT_HOST=0x10, FAULT_PWM=0x20 };
enum { SYS_BOOT=0, SYS_RUNNING=1, SYS_SHUTDOWN=2, SYS_FAULT=3, SYS_UPGRADE=4 };
enum { PAGE_STATUS=0, PAGE_DISKS=1, PAGE_FAULT=2, PAGE_BOOT=3, PAGE_LCD_TEST=4 };
/* LCD diagnostics: sequence, schema=1, flags, ID[3], status[4].
 * Read block then sequence again; accept equal even sequence and schema=1.
 * Captured on LCD initialization, not a live optical/connection indication.
 * Reset flags describe MCU input sampling, not the connector voltage. */
/* Two network records: flags, Mbps low, Mbps high. Commit flags last.
 * Independent changing heartbeat must arrive within five seconds. */
/* MCU health: seq, schema=1, flags, payload bytes=80, 80-byte LE record.
 * Read even seq, entire 84-byte block, seq again; accept equal even seq.
 * Record CRC32C covers payload bytes0..75; payload CRC is bytes76..79.
 * Boot snapshot, not a current sensor or live fault status. Invalid payload
 * is zeroed with VALID clear; absent schema on older firmware unsupported.
 * No writes/clear/fault-injection command exists in this range. */
#define Y4_HEALTH_LEN 84
#define Y4_HEALTH_SCHEMA 1
#define Y4_HEALTH_VALID 1
#define Y4_HEALTH_FAULT_PRESENT 2
#define Y4_HEALTH_FRAME_UNTRUSTED 4
#define Y4_NET_STRIDE 3
#define SLOT_UNKNOWN 0
#define SLOT_EMPTY 1
#define SLOT_ONLINE 2
/* 0.1.29: protocol2 fixed state bytes 0x74..0x77, order J1/J10/J4/J12.
 * Display observations only, never controls or physical rail/PG proof. Type
 * bytes 0x78..0x7b retain their original identity meaning; shown only ONLINE.
 * EMPTY means verified unpopulated, LOST requires prior expected-online
 * evidence from the host. The MCU never infers EMPTY/LOST from a zero bit. */
#define SLOT_HELD 3
#define SLOT_LOST 4
#define SLOT_FAULT 5
#define SLOT_TRANSITION 6
/* Per-slot MCU-local freshness; host heartbeat never refreshes these bytes. */
#define Y4_SLOT_STATE_LIFE_MS 3000u
#define NET_VALID 1
#define NET_LINK 2
#define NET_ACTIVITY 4
#define NET_SPEED_VALID 8
#define Y4_LCD_DIAG_LEN 10
#define LCD_DIAG_TRANSFER 1
#define LCD_DIAG_RESET_LOW 2
#define LCD_DIAG_RESET_HIGH 4
/* Backend diagnostics (frozen wire, MCU 0.1.24, protocol 2): R_BACKENDS
 * block of exactly Y4_BACKENDS_LEN=28 bytes, little endian, read-only,
 * published by the firmware at most once per second as seq odd while
 * the payload changes and seq even when stable (read the block, then
 * seq again; accept identical even values). Layout:
 *   0  u8  seq (odd changing / even stable)
 *   1  u8  schema = Y4_BACKENDS_SCHEMA
 *   2  u8  build flags: bit0 SPI/DMA transport compiled, bit1 hardware
 *      fan PWM compiled, all other bits zero
 *   3  u8  runtime flags: bit0 SPI configured, bit1 SPI active,
 *      bit2 SPI suspended, bit3 SPI fault_lock, bit4 SPI dma_stuck,
 *      bit5 counters saturated, bit6 VALID coherent frame, bit7 zero
 *   4  u32 actual SPI SCK Hz (0 = unconfigured)
 *   8  u32 COMPLETED SUCCESSFUL DMA transfers (successful DMA
 *      completion AND SPI idle AND proven DMA disable; never attempts)
 *   12 u32 saturating sum of SPI timeout/dma_error/spi_error events
 *   16 u32 actual SPI success bytes (fully completed transfers)
 *   20 i16 current SPI status, 22 i16 last SPI status (errno scale
 *      -4095..0)
 *   24 i16 hardware PWM status (1 active / 0 disabled / negative)
 *   26 u16 actual hardware PWM frequency Hz (0 = unavailable/disabled/
 *      fault; never guessed)
 * VALID means a coherent diagnostic frame, NOT that both backends are
 * enabled/healthy: consult the build and status flags. A compiled-out
 * backend reports compiled=0 and never fakes activity or frequency.
 * No MMIO addresses and no debug write commands exist in this range. */
#define Y4_BACKENDS_LEN 28
#define Y4_BACKENDS_SCHEMA 1
#define Y4_BACKENDS_BUILD_SPI 1u  /* hardware SPI/DMA transport compiled */
#define Y4_BACKENDS_BUILD_PWM 2u  /* hardware fan PWM backend compiled  */
#define Y4_BACKENDS_SPI_CONFIGURED 1u
#define Y4_BACKENDS_SPI_ACTIVE 2u
#define Y4_BACKENDS_SPI_SUSPENDED 4u
#define Y4_BACKENDS_SPI_FAULT_LOCK 8u
#define Y4_BACKENDS_SPI_DMA_STUCK 16u
#define Y4_BACKENDS_COUNTERS_SATURATED 32u
#define Y4_BACKENDS_VALID 64u
/* Snapshot counter brackets uptime/RPM/fault publication. Read even S,
 * payload, S again; accept only identical even counters. One-byte writes. */

/* Additive display-only stream (MCU 0.1.34, protocol 2). R_VIEW_SCHEMA=1 is
 * the capability: it is read-only and occupies previously unused bytes.
 * BEGIN accepts a nonzero token different from the last accepted BEGIN.
 * Write exactly Y4_VIEW_FRAME_LEN single bytes to DATA (the register does
 * not auto-repeat within a multi-byte transaction), then COMMIT the token.
 * CRC8 poly07/init0 covers token followed by frame bytes0..30; byte31 is
 * the CRC. Only a complete, validated frame changes the displayed view.
 * Partial streams expire after2000ms and never refresh the previous view.
 *
 * Payload31, physical order LAN/SFP1/SFP2, logical order LAN/Bond:
 *   0 schema1; 1 known mask; 2 present mask; 3 carrier mask (all3 bits)
 *   4 LAN state; 5 Bond state; 6 IPv4-valid mask (2bits); 7 actual egress
 *   8..11 LAN IPv4, 12..15 Bond IPv4 (network byte order)
 *   16 initialization state; 17 attempt0..5; 18 max_attempts=5; 19 actual Bond state
 *   20 i16 init error LE; 22 u16 network sample age ms LE
 *   24 i16 network error LE; 26 flags(bit0 init history valid); 27..28
 *   LAN/Bond prefix lengths; 29 active member mask; 30 LACP member mask.
 *   Bond masks use physical SFP1bit1/SFP2bit2; no LAN bit. LACP_MULTI
 *   requires two actually active collecting/distributing members of the
 *   same aggregator; two physical carriers alone cannot establish it.
 *
 * Network observations expire after3000ms minus host age and stream time;
 * expiration clears bothIPs, physical masks and actual-egress indication.
 * The last initialization receipt/count is boot history and is retained,
 * explicitly separate from live network/MCU health. This stream cannot
 * change temperatures, fan duties, identity, firmware or any NIC setting.
 * The frozen health0x90..0xe3 and backend0xe4..0xff blocks are unchanged. */
enum {
 R_VIEW_BEGIN=0x06, R_VIEW_DATA=0x07, R_VIEW_COMMIT=0x08,
 R_VIEW_STATUS=0x4e, R_VIEW_SCHEMA=0x4f
};
#define Y4_VIEW_SCHEMA 1u
#define Y4_VIEW_PAYLOAD_LEN 31u
#define Y4_VIEW_FRAME_LEN 32u
#define Y4_VIEW_PARTIAL_MS 2000u
#define Y4_VIEW_LIFE_MS 3000u
#define Y4_VIEW_INIT_VALID 1u
enum { VIEW_IDLE=0,VIEW_PENDING=1,VIEW_COMMITTED=2,
       VIEW_REJECTED=3,VIEW_EXPIRED=4 };
enum { VIEW_NET_UNKNOWN=0,VIEW_NET_DISCONNECTED=1,VIEW_NET_WAITING=2,
       VIEW_NET_NO_ADDRESS=3,VIEW_NET_READY=4,VIEW_NET_FAILED=5,
       VIEW_NET_INITIALIZING=6 };
enum { VIEW_EGRESS_NONE=0,VIEW_EGRESS_LAN=1,VIEW_EGRESS_BOND=2,
       VIEW_EGRESS_OTHER=3,VIEW_EGRESS_AMBIGUOUS=4 };
enum { VIEW_BOND_UNKNOWN=0,VIEW_BOND_DOWN=1,VIEW_BOND_NEGOTIATING=2,
       VIEW_BOND_INDIVIDUAL=3,VIEW_BOND_LACP_SINGLE=4,VIEW_BOND_LACP_MULTI=5 };
enum { VIEW_INIT_UNKNOWN=0,VIEW_INIT_CHECKING=1,VIEW_INIT_MCU=2,
       VIEW_INIT_NIC=3,VIEW_INIT_READY=4,VIEW_INIT_FAILED=5,
       VIEW_INIT_CANCELLED=6 };

#endif
