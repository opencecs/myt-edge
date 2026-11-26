/* SPDX-License-Identifier: Apache-2.0 */
#ifndef Y4_BOARD_DAEMON_H
#define Y4_BOARD_DAEMON_H
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>
#include <signal.h>
extern volatile sig_atomic_t y4_board_stopping;
#include "../network/y4_network.h"
#define Y4_BOARD_RUNTIME "/run/y4-board"
#define Y4_BOARD_STATE "/var/lib/y4-board"
#define Y4_BOARD_SOCKET Y4_BOARD_RUNTIME "/api.sock"
#define Y4_BOARD_PACKAGE_LIMIT 1049736u
#define Y4_BOARD_MAINTAIN "/usr/libexec/y4-board/y4-mcu-maintain"
#define Y4_BOARD_KEY "/usr/share/y4-board/mcu-public-key.bin"
#define Y4_BOARD_HOST_VERSION "/etc/y4-board/host-version"
#define Y4_BOARD_INIT_STATUS Y4_BOARD_RUNTIME "/initialization.json"
#define Y4_BOARD_SAMPLE_MAGIC UINT32_C(0x59423453)
struct y4_board_temperature { int valid,error; int64_t millicelsius;
 /* Age of the SENSOR MEASUREMENT itself, not of the monitor snapshot.
  * 0 means the value was measured during the current cycle; a cached
  * source (pcie switch) reports its real cache age. Only meaningful
  * together with valid=1. */
 uint32_t source_age_ms; };
/* Read-only network-manager phase observation for one logical interface.
 * valid=0 means the manager state was not observable this cycle; state then
 * carries no evidence and every consumer must treat it as unknown. */
struct y4_network_manager_state { int valid,state; };
struct y12_board_fan_sample {
 uint64_t sampled_ms;
 uint64_t pwm_sampled_ms[2];
 int pwm_raw[2];
 int rpm[6];
};
struct y4_board_sample {
 uint32_t magic;
 uint64_t sampled_ms,completed_ms;
 int mcu_error,maintenance,temperature_error,cooling_error,fan_error;
 int health_error,backend_error,slots_error,activity_error,display_error;
 uint8_t firmware[3],uid[12],uid_valid,fault_bits,fan_mask,tach_mask;
 uint8_t requested_pwm,effective_pwm[4],slot_online,slot_active,slot_type[4];
 uint16_t rpm[4];
 struct y4_board_temperature cpu,inside,pcie_switch;
 uint32_t reset_flags,fault_count,communication_errors,minimum_stack_pointer;
 uint32_t pwm_hz,spi_hz,dma_completed,backend_error_events;
 struct y4_network_snapshot network;
 struct y4_network_manager_state nm[Y4_NET_LOGICAL_COUNT];
 /* MCU fan policy readback (MCU >=0.1.39; fan_policy_error=-EOPNOTSUPP on
 * older firmware, mode/status then carry no evidence). mode/status are the
 * MCU registers 0x73/0x72 verbatim: 0=legacy 1=auto 2=manual; status
 * 0=none 1=persisting 2=committed 3=failed. duty[] = stored FAN3/4/5
 * duties in percent. FAN1 is the kernel pwm-fan hwmon tach, read-only. */
 int fan_policy_error,fan1_error,fan2_error;
 uint8_t fan_policy_mode,fan_policy_status,fan_policy_duty[3];
 uint8_t fan1_valid,fan2_valid;
 uint16_t fan1_rpm,fan2_rpm;
 struct y12_board_fan_sample y12_fans;
 int power_error;
 uint16_t y12_present_valid,y12_present;
 uint16_t power_valid,power_sequence,voltage_mv[12];
 int16_t current_ma[12],temperature_dc[12];
 uint16_t temperature_valid,y12_slot_online,slot_led_valid,slot_led_on;
};
struct y4_board_initialization {
 int valid,attempt,max_attempts,status,completed,network_safe;
 char phase[40];
};
uint64_t y4_board_now_ms(void);
int y4_board_private_dir(const char *);
int y4_board_read_file(const char *,char *,size_t,size_t *);
int y4_board_atomic_file(const char *,const char *,const void *,size_t,mode_t);
int y4_board_json_number(const char *,const char *,long long *);
int y4_board_json_unsigned(const char *,const char *,uint64_t *);
int y4_board_json_bool(const char *,const char *,int *);
int y4_board_json_string(const char *,const char *,char *,size_t);
int y4_board_initialization_read(struct y4_board_initialization *);
int y4_board_nm_states(struct y4_network_manager_state [Y4_NET_LOGICAL_COUNT]);
int y4_board_sample_json(const struct y4_board_sample *,char *,size_t);
int y4_board_version_json(const struct y4_board_sample *,char *,size_t);
int y12_board_set_fan(unsigned group,unsigned percent,struct y4_board_sample *);
void y12_board_fans_sample(struct y4_board_sample *);
void y12_board_fans_preserve_readback(const struct y4_board_sample *,struct y4_board_sample *);
unsigned y12_board_fans_fresh_mask(const struct y4_board_sample *,uint64_t now);
int y12_board_power_json(const struct y4_board_sample *,char *,size_t);
int y4_board_fans_json(const struct y4_board_sample *,char *,size_t);
int y4_board_temperature_json(const struct y4_board_sample *,char *,size_t);
int y4_board_running_slot(void);
int y4_board_slot_pending_reboot(void);
int y4_board_ota_slot_ready(void);
int y4_board_slot_boot_count(int allow_rollback);
int y4_board_request_reboot(void);
int y4_board_ota_busy(void);
int y4_board_ota_admit(size_t);
int y4_board_ota_write(const char *,size_t);
int y4_board_ota_finish(void);
void y4_board_ota_abort(void);
void y4_board_ota_shutdown(void);
void y4_board_ota_tick(void);
int y4_board_ota_phase(void);
int y4_board_ota_status_json(char *,size_t);
int y4_board_ota_confirm(void);
int y4_board_kernels_gc(void);
int y4_board_slot_confirm_tick(const struct y4_board_sample *);
int y4_board_monitor_run(int);
pid_t y4_board_pcie_start(void);
int y4_board_pcie_tick(pid_t *);
int y4_board_pcie_stop(pid_t *);
void y4_board_pcie_child_close(void);
#endif
