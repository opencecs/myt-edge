/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include "board_daemon.h"
#include <errno.h>
#include <glob.h>
#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <sys/utsname.h>
struct text {char *data;size_t cap,used;int error;};
static void put(struct text *t,const char *fmt,...) {
 if(t->error)return;va_list a;va_start(a,fmt);
 int n=vsnprintf(t->data+t->used,t->cap-t->used,fmt,a);va_end(a);
 if(n<0||(size_t)n>=t->cap-t->used)t->error=-EOVERFLOW;else t->used+=(size_t)n;
}
static const char *boolean(int v){return v?"true":"false";}
/* FAULT_HOST bit of the MCU fault byte: the host heartbeat timed out. */
#define Y4_MCU_FAULT_HOST 0x10u
static const char *policy_mode_name(unsigned mode) {
 return mode==1u?"auto":mode==2u?"manual":"legacy";
}
/* Shared fan-policy object; MCU <0.1.39 reports error=-EOPNOTSUPP and the
 * consumers must treat mode/status/stored as carrying no evidence. */
static void policy_object(struct text*t,const struct y4_board_sample*s,int fresh) {
 int ok=fresh&&!s->fan_policy_error;
 put(t,"\"policy\":{\"mode\":\"%s\",\"status\":%d,\"stored\":[%u,%u,%u],\"host_online\":%s,\"error\":%d}",
 ok?policy_mode_name(s->fan_policy_mode):"unknown",
 ok?(int)s->fan_policy_status:0,
 ok?s->fan_policy_duty[0]:0u,ok?s->fan_policy_duty[1]:0u,ok?s->fan_policy_duty[2]:0u,
 boolean(fresh&&!(s->fault_bits&Y4_MCU_FAULT_HOST)),
 fresh?s->fan_policy_error:-ESTALE);
}
static void soc_fan_objects(struct text*t,const struct y4_board_sample*s,int fresh) {
 int fan1_ok=fresh&&!s->fan1_error&&s->fan1_valid;
 int fan2_ok=fresh&&!s->fan2_error&&s->fan2_valid;
 put(t,"\"FAN1\":{\"valid\":%s,\"status\":%d,\"rpm\":%u},"
       "\"FAN2\":{\"valid\":%s,\"status\":%d,\"rpm\":%u}",
 boolean(fan1_ok),fresh?s->fan1_error:-ESTALE,fan1_ok?s->fan1_rpm:0u,
 boolean(fan2_ok),fresh?s->fan2_error:-ESTALE,fan2_ok?s->fan2_rpm:0u);
}
static void temperature(struct text*t,const char*name,const struct y4_board_temperature*s,int fresh) {
 put(t,"\"%s\":{\"valid\":%s,\"status\":%d,\"millicelsius\":",name,boolean(fresh&&s->valid),fresh?s->error:-ESTALE);
 if(fresh&&s->valid)put(t,"%" PRId64,s->millicelsius);else put(t,"null");
 /* source_age_ms is the sensor measurement age, not the snapshot refresh
  * age; 0 means measured during the current monitor cycle. */
 put(t,",\"source_age_ms\":%u}",fresh&&s->valid?s->source_age_ms:0u);
}
int y4_board_sample_json(const struct y4_board_sample *s,char *out,size_t cap) {
 if(!s||!out||cap<128u)return -EINVAL;
 struct text t={out,cap,0,0};uint64_t now=y4_board_now_ms();
 int fresh=s->magic==Y4_BOARD_SAMPLE_MAGIC&&s->sampled_ms&&now>=s->sampled_ms&&now-s->sampled_ms<5000u;
 uint64_t age=s->sampled_ms&&now>=s->sampled_ms?now-s->sampled_ms:0;
 char uid[25]={0},fw[24]={0};if(fresh&&!s->mcu_error){(void)snprintf(fw,sizeof fw,"%u.%u.%u",s->firmware[0],s->firmware[1],s->firmware[2]);}
 if(fresh&&s->uid_valid)for(unsigned i=0;i<12u;i++)(void)snprintf(uid+i*2u,3,"%02x",s->uid[i]);
 put(&t,"{\"schema\":1,\"device\":\"%s\",\"sampled_ms\":%" PRIu64 ",\"age_ms\":%" PRIu64 ",\"fresh\":%s,\"maintenance\":%s,",
 Y4_DEVICE_ID,s->sampled_ms,age,boolean(fresh),boolean(fresh&&s->maintenance));
 /* Release manifest: the build bakes /etc/y4-release.json; embed its
  * body verbatim so the snapshot carries every component version
  * without a JSON parser here. kernel_running comes from uname and
  * should match the baked kernel of the active slot. */
 {
  static char inner[2048];static int have;static int once;static int ilen;
  if(!once){once=1;FILE*f=fopen("/etc/y4-release.json","rb");
   if(f){char raw[2048];size_t n=fread(raw,1,sizeof raw-1,f);fclose(f);raw[n]=0;
    if(n&&raw[0]=='{'){char*close=strrchr(raw,'}');
     if(close){int m=(int)(close-raw-1);if(m>(int)sizeof inner-1)m=(int)sizeof inner-1;
      memcpy(inner,raw+1,(size_t)m);inner[m]=0;ilen=m;have=1;}}}}
  struct utsname un={0};char running[128]={0};
  if(uname(&un)==0)(void)snprintf(running,sizeof running,"%s",un.release);
  if(have&&inner[strspn(inner," \t\r\n")])put(&t,"\"versions\":{%.*s,\"kernel_running\":\"%s\"},",ilen,inner,running);
  else put(&t,"\"versions\":{\"kernel_running\":\"%s\"},",running);
 }
 put(&t,"\"mcu\":{\"status\":%d,\"firmware\":\"%s\",\"uid_valid\":%s,\"uid\":\"%s\",\"fan_status\":%d,\"fault_bits\":%u},",
 fresh?s->mcu_error:-ESTALE,fw,boolean(fresh&&s->uid_valid),uid,fresh?s->fan_error:-ESTALE,s->fault_bits);
 put(&t,"\"temperatures\":{");temperature(&t,"CPU",&s->cpu,fresh);put(&t,",");
 temperature(&t,"inside",&s->inside,fresh);put(&t,",");temperature(&t,"pcie_switch",&s->pcie_switch,fresh);put(&t,"},");
 put(&t,"\"cooling\":{\"status\":%d,\"requested_pwm\":%u,\"FAN1_owner\":\"linux\",",fresh?s->cooling_error:-ESTALE,s->requested_pwm);
 soc_fan_objects(&t,s,fresh);put(&t,",\"MCU_fans\":[");
 for(unsigned i=0;i<4;i++)put(&t,"%s{\"connector\":\"FAN%u\",\"valid\":%s,\"monitored\":%s,\"rpm\":%u,\"effective_pwm\":%u}",
 i?",":"",i+3u,boolean(fresh&&!s->fan_error),boolean(!!(s->tach_mask&(1u<<i))),s->rpm[i],s->effective_pwm[i]);
 put(&t,"],");policy_object(&t,s,fresh);
 put(&t,"},\"health\":{\"status\":%d,\"reset_flags\":%u,\"fault_count\":%u,\"communication_errors\":%u,\"minimum_stack_pointer\":%u},",
 fresh?s->health_error:-ESTALE,s->reset_flags,s->fault_count,s->communication_errors,s->minimum_stack_pointer);
 put(&t,"\"backends\":{\"status\":%d,\"pwm_hz\":%u,\"spi_hz\":%u,\"dma_completed\":%u,\"error_events\":%u},",
 fresh?s->backend_error:-ESTALE,s->pwm_hz,s->spi_hz,s->dma_completed,s->backend_error_events);
 put(&t,"\"slots\":{\"status\":%d,\"online_mask\":%u,\"activity_status\":%d,\"activity_mask\":%u,\"type\":[%u,%u,%u,%u]},",
 fresh?s->slots_error:-ESTALE,s->slot_online,fresh?s->activity_error:-ESTALE,s->slot_active,s->slot_type[0],s->slot_type[1],s->slot_type[2],s->slot_type[3]);
 const struct y4_network_snapshot*n=&s->network;
 int netfresh=fresh&&n->sampled_ms&&now>=n->sampled_ms&&now-n->sampled_ms<5000u;
 put(&t,"\"network\":{\"status\":%d,\"address_status\":%d,\"route_status\":%d,\"default_egress\":%u,\"physical\":[",
 netfresh?n->error:-ESTALE,netfresh?n->address_error:-ESTALE,netfresh?n->route_error:-ESTALE,netfresh?(unsigned)n->default_egress:0u);
 static const char *const role[]={"LAN_2G5","SFP1_10G","SFP2_10G"};
 for(unsigned i=0;i<3u;i++){const struct y4_network_link*l=&n->physical[i];
  int mac_ok=netfresh&&l->mac_valid;
  int counters_ok=netfresh&&!l->counters_error;
  char mac[24]={0};
  if(mac_ok)snprintf(mac,sizeof mac,"%02x:%02x:%02x:%02x:%02x:%02x",
   l->mac[0],l->mac[1],l->mac[2],l->mac[3],l->mac[4],l->mac[5]);
  put(&t,"%s{\"role\":\"%s\",\"valid\":%s,\"present\":%s,\"carrier\":%s,\"status\":%d,\"ifindex\":%u,\"speed_mbps\":%u,\"mac\":\"%s\","
  "\"rx_bytes\":%" PRIu64 ",\"tx_bytes\":%" PRIu64 ",\"rx_packets\":%" PRIu64 ",\"tx_packets\":%" PRIu64 ",\"counters_error\":%d}",
  i?",":"",role[i],boolean(netfresh&&l->valid),boolean(netfresh&&l->present),boolean(netfresh&&l->valid&&l->carrier),netfresh?l->error:-ESTALE,l->ifindex,l->speed_mbps,mac_ok?mac:"",
  counters_ok?l->rx_bytes:0u,counters_ok?l->tx_bytes:0u,counters_ok?l->rx_packets:0u,counters_ok?l->tx_packets:0u,
  netfresh?l->counters_error:-ESTALE);}
 put(&t,"],\"bond\":{\"mode\":%u,\"state\":%u,\"state_status\":%d,\"ad_info_valid\":%s,\"partner_present\":%s,"
 "\"aggregator_id\":%u,\"aggregator_ports\":%u,\"partner_key\":%u,\"active_mask\":%u,\"lacp_mask\":%u},\"logical\":[",
 netfresh?n->bond_mode:0u,netfresh?(unsigned)n->bond_state:0u,netfresh?n->bond_state_error:-ESTALE,
 boolean(netfresh&&n->bond_ad_info_valid),boolean(netfresh&&n->bond_partner_present),
 n->bond_aggregator_id,n->bond_aggregator_ports,n->bond_partner_key,netfresh?n->bond_active_mask:0u,netfresh?n->bond_lacp_mask:0u);
 for(unsigned i=0;i<2u;i++){const struct y4_network_interface*l=&n->logical[i];int valid=netfresh&&l->ipv4_valid&&!l->ipv4_error;
  int gw_ok=netfresh&&l->gateway_valid;
  int counters_ok=netfresh&&!l->link.counters_error;
  put(&t,"%s{\"role\":\"%s\",\"link_valid\":%s,\"carrier\":%s,\"address_valid\":%s,\"address_status\":%d,\"ipv4\":\"%s\",\"prefix_length\":%u,\"gateway\":\"%s\",\"manager_state_known\":%s,\"manager_state\":%d,"
  "\"rx_bytes\":%" PRIu64 ",\"tx_bytes\":%" PRIu64 ",\"rx_packets\":%" PRIu64 ",\"tx_packets\":%" PRIu64 ",\"counters_error\":%d}",
  i?",":"",i?"BOND_10G":"LAN_2G5",boolean(netfresh&&l->link.valid),boolean(netfresh&&l->link.valid&&l->link.carrier),
  boolean(valid),netfresh?l->ipv4_error:-ESTALE,valid?l->ipv4:"",valid?l->prefix_length:0u,
  gw_ok?l->gateway:"",boolean(s->nm[i].valid),s->nm[i].state,
  counters_ok?l->link.rx_bytes:0u,counters_ok?l->link.tx_bytes:0u,counters_ok?l->link.rx_packets:0u,counters_ok?l->link.tx_packets:0u,
  netfresh?l->link.counters_error:-ESTALE);}
 put(&t,"]},\"display_status\":%d,",fresh?s->display_error:-ESTALE);
 struct y4_board_initialization init;int e=y4_board_initialization_read(&init);
 put(&t,"\"initialization\":{\"valid\":%s,\"receipt_status\":%d,\"attempt\":%d,\"max_attempts\":5,\"status\":%d,\"completed\":%s,\"network_safe\":%s,\"phase\":\"%s\"}}\n",
 boolean(!e&&init.valid),e,init.attempt,init.status,boolean(!e&&init.completed),boolean(!e&&init.network_safe),!e?init.phase:"unavailable");
 return t.error?t.error:(int)t.used;
}
int y4_board_temperature_json(const struct y4_board_sample *s,char *out,size_t cap) {
 if(!s||!out||cap<128u)return -EINVAL;
 struct text t={out,cap,0,0};uint64_t now=y4_board_now_ms();
 int fresh=s->magic==Y4_BOARD_SAMPLE_MAGIC&&s->sampled_ms&&now>=s->sampled_ms&&now-s->sampled_ms<5000u;
 uint64_t age=s->sampled_ms&&now>=s->sampled_ms?now-s->sampled_ms:0;
 put(&t,"{\"schema\":1,\"device\":\"%s\",\"sampled_ms\":%" PRIu64 ",\"age_ms\":%" PRIu64 ",\"fresh\":%s,\"temperatures\":{",
 Y4_DEVICE_ID,s->sampled_ms,age,boolean(fresh));
 temperature(&t,"CPU",&s->cpu,fresh);put(&t,",");
 temperature(&t,"inside",&s->inside,fresh);put(&t,",");
 temperature(&t,"pcie_switch",&s->pcie_switch,fresh);
 put(&t,"}}\n");
 return t.error?t.error:(int)t.used;
}
/* Resolve the hwmon device by DT node, never by hwmon allocation order. */
static const char *const y12_fan_nodes[] = {"pwm-fan-out0", "pwm-fan-out2"};
static const unsigned y12_tach_groups[] = {1, 0, 0, 1, 0, 1};
static const char *const y12_tach_attributes[] = {
 "fan1_input", "fan1_input", "fan2_input", "fan2_input", "fan3_input", "fan3_input",
};

static int y12_fan_hwmon(const char *node, char *directory, size_t cap)
{
 char pattern[160];
 glob_t matches = {0};
 snprintf(pattern, sizeof pattern, "/sys/bus/platform/devices/%s/hwmon/hwmon*", node);
 int result = -ENODEV;
 if (!glob(pattern, 0, NULL, &matches) && matches.gl_pathc == 1) {
  size_t length = strlen(matches.gl_pathv[0]);
  if (length >= cap)
   result = -EOVERFLOW;
  else {
   memcpy(directory, matches.gl_pathv[0], length + 1);
   result = 0;
  }
 }
 globfree(&matches);
 return result;
}

static int y12_fan_path(const char *directory, const char *attribute,
                        char *path, size_t cap)
{
 int length = snprintf(path, cap, "%s/%s", directory, attribute);
 return length > 0 && (size_t)length < cap ? 0 : -EOVERFLOW;
}

static int y12_fan_read(const char *directory, const char *attribute)
{
 char path[256];
 int error = y12_fan_path(directory, attribute, path, sizeof path);
 if (error)
  return error;
 FILE *file = fopen(path, "re");
 if (!file)
  return -errno;
 int value;
 int result = fscanf(file, "%d", &value) == 1 && value >= 0 ? value : -EIO;
 fclose(file);
 return result;
}

void y12_board_fans_sample(struct y4_board_sample *sample)
{
 struct y12_board_fan_sample *fans = &sample->y12_fans;
 char directories[2][256];
 int errors[2];
 fans->sampled_ms = y4_board_now_ms();
 for (unsigned i = 0; i < 2; i++) {
  errors[i] = y12_fan_hwmon(y12_fan_nodes[i], directories[i], sizeof directories[i]);
  fans->pwm_sampled_ms[i] = fans->sampled_ms;
  fans->pwm_raw[i] = errors[i] ? errors[i] : y12_fan_read(directories[i], "pwm1");
 }
 for (unsigned i = 0; i < 6; i++) {
  unsigned group = y12_tach_groups[i];
  fans->rpm[i] = errors[group] ? errors[group] :
                 y12_fan_read(directories[group], y12_tach_attributes[i]);
 }
}

int y12_board_set_fan(unsigned group, unsigned percent, struct y4_board_sample *sample)
{
 if (strcmp(Y4_DEVICE_ID, "Y12"))
  return -EOPNOTSUPP;
 if (!sample || group >= 2 || percent > 100)
  return -EINVAL;
 char directory[256], path[256];
 int error = y12_fan_hwmon(y12_fan_nodes[group], directory, sizeof directory);
 if (error)
  return error;
 error = y12_fan_path(directory, "pwm1", path, sizeof path);
 if (error)
  return error;
 int raw = (int)((percent * 255 + 50) / 100);
 FILE *file = fopen(path, "we");
 if (!file)
  return -errno;
 int failed = fprintf(file, "%d\n", raw) < 0;
 if (fclose(file) || failed)
  return -EIO;
 int actual = y12_fan_read(directory, "pwm1");
 if (actual < 0)
  return actual;
 if (actual != raw)
  return -EIO;
 sample->y12_fans.pwm_raw[group] = actual;
 sample->y12_fans.pwm_sampled_ms[group] = y4_board_now_ms();
 return 0;
}

void y12_board_fans_preserve_readback(const struct y4_board_sample *previous,
                                      struct y4_board_sample *next)
{
 if (strcmp(Y4_DEVICE_ID, "Y12"))
  return;
 /* The monitor stamps the start of its read. It may report an older PWM
  * after POST has already written and read back a newer one in the daemon. */
 for (unsigned i = 0; i < 2; i++) {
  uint64_t sampled = previous->y12_fans.pwm_sampled_ms[i];
  if (sampled && sampled >= next->y12_fans.pwm_sampled_ms[i]) {
   next->y12_fans.pwm_raw[i] = previous->y12_fans.pwm_raw[i];
   next->y12_fans.pwm_sampled_ms[i] = sampled;
  }
 }
}

static int y12_fan_fresh(uint64_t now, uint64_t sampled)
{
 return sampled && now >= sampled && now - sampled < 5000u;
}

enum {
 Y12_FAN_TACH_FRESH = 1u << 0,
 Y12_FAN_PWM0_FRESH = 1u << 1,
};

unsigned y12_board_fans_fresh_mask(const struct y4_board_sample *s, uint64_t now)
{
 const struct y12_board_fan_sample *fans = &s->y12_fans;
 unsigned mask = s->magic == Y4_BOARD_SAMPLE_MAGIC && y12_fan_fresh(now, fans->sampled_ms)
               ? Y12_FAN_TACH_FRESH : 0u;
 for (unsigned i = 0; i < 2; i++)
  if (y12_fan_fresh(now, fans->pwm_sampled_ms[i]))
   mask |= Y12_FAN_PWM0_FRESH << i;
 return mask;
}

static int y12_fans_json(const struct y4_board_sample *s, char *out, size_t cap)
{
 const struct y12_board_fan_sample *fans = &s->y12_fans;
 uint64_t now = y4_board_now_ms(), sampled = fans->sampled_ms;
 unsigned fresh_mask = y12_board_fans_fresh_mask(s, now);
 int fresh = !!(fresh_mask & Y12_FAN_TACH_FRESH);
 uint64_t age = sampled && now >= sampled ? now - sampled : 0;
 struct text t = {.data = out, .cap = cap};
 put(&t, "{\"schema\":1,\"device\":\"Y12\",\"default_percent\":20,"
         "\"sampled_ms\":%" PRIu64 ",\"age_ms\":%" PRIu64 ",\"fresh\":%s,\"groups\":[",
     sampled, age, boolean(fresh));
 for (unsigned i = 0; i < 2; i++) {
  uint64_t pwm_sampled = fans->pwm_sampled_ms[i];
  int pwm = fans->pwm_raw[i];
  put(&t, "%s{\"id\":\"%s\",\"pwm_sampled_ms\":%" PRIu64 ",\"pwm_raw\":",
      i ? "," : "", y12_fan_nodes[i], pwm_sampled);
  if ((fresh_mask & (Y12_FAN_PWM0_FRESH << i)) && pwm >= 0 && pwm <= 255)
   put(&t, "%d", pwm);
  else
   put(&t, "null");
  put(&t, "}");
 }
 put(&t, "],\"fans\":[");
 for (unsigned i = 0; i < 6; i++) {
  unsigned group = y12_tach_groups[i];
  put(&t, "%s{\"id\":\"FAN%u\",\"group\":\"%s\",\"rpm\":",
      i ? "," : "", i + 1, y12_fan_nodes[group]);
  if (fresh && fans->rpm[i] >= 0)
   put(&t, "%d", fans->rpm[i]);
  else
   put(&t, "null");
  put(&t, "}");
 }
 put(&t, "]}\n");
 return t.error ? t.error : (int)t.used;
}

int y4_board_fans_json(const struct y4_board_sample *s,char *out,size_t cap) {
 if(!s||!out||cap<256u)return -EINVAL;
 if(!strcmp(Y4_DEVICE_ID,"Y12"))return y12_fans_json(s,out,cap);
 struct text t={out,cap,0,0};uint64_t now=y4_board_now_ms();
 int fresh=s->magic==Y4_BOARD_SAMPLE_MAGIC&&s->sampled_ms&&now>=s->sampled_ms&&now-s->sampled_ms<5000u;
 uint64_t age=s->sampled_ms&&now>=s->sampled_ms?now-s->sampled_ms:0;
 put(&t,"{\"schema\":1,\"device\":\"%s\",\"sampled_ms\":%" PRIu64 ",\"age_ms\":%" PRIu64 ",\"fresh\":%s,",
 Y4_DEVICE_ID,s->sampled_ms,age,boolean(fresh));
 policy_object(&t,s,fresh);put(&t,",");
 soc_fan_objects(&t,s,fresh);put(&t,",\"MCU_fans\":[");
 for(unsigned i=0;i<4;i++)put(&t,"%s{\"connector\":\"FAN%u\",\"valid\":%s,\"monitored\":%s,\"rpm\":%u,\"effective_pwm\":%u}",
 i?",":"",i+3u,boolean(fresh&&!s->fan_error),boolean(!!(s->tach_mask&(1u<<i))),s->rpm[i],s->effective_pwm[i]);
 put(&t,"]}\n");
 return t.error?t.error:(int)t.used;
}


int y12_board_power_json(const struct y4_board_sample *s,char *out,size_t cap)
{
 uint64_t now=y4_board_now_ms();
 int error=s->magic==Y4_BOARD_SAMPLE_MAGIC&&s->sampled_ms&&now>=s->sampled_ms&&
           now-s->sampled_ms<5000u?s->power_error:-ESTALE;
 struct text t={.data=out,.cap=cap};
 int64_t total_uw=0;
 unsigned valid=0;
 put(&t,"{\"schema\":1,\"device\":\"Y12\",\"error\":%d,\"slots\":[",error);
 for(unsigned i=0;i<12;i++) {
  put(&t,"%s{\"slot\":%u,\"online\":",i?",":"",i+1);
  if(s->slots_error||s->magic!=Y4_BOARD_SAMPLE_MAGIC||!s->sampled_ms||
     now<s->sampled_ms||now-s->sampled_ms>=5000u)put(&t,"null");
  else put(&t,"%s",boolean(s->y12_slot_online&(1u<<i)));
  put(&t,",\"present\":");
  if(s->magic==Y4_BOARD_SAMPLE_MAGIC&&s->sampled_ms&&now>=s->sampled_ms&&
     now-s->sampled_ms<5000u&&(s->y12_present_valid&(1u<<i)))
   put(&t,"%s",boolean(s->y12_present&(1u<<i)));
  else put(&t,"null");
  put(&t,",\"run_led\":");
  if(s->magic==Y4_BOARD_SAMPLE_MAGIC&&s->sampled_ms&&now>=s->sampled_ms&&
     now-s->sampled_ms<5000u&&(s->slot_led_valid&(1u<<i)))
   put(&t,"%s",boolean(s->slot_led_on&(1u<<i)));
  else put(&t,"null");
  put(&t,",\"temperature_dc\":");
  if(!error&&(s->temperature_valid&(1u<<i)))put(&t,"%d",s->temperature_dc[i]);
  else put(&t,"null");
  put(&t,",");
  if(!error&&(s->power_valid&(1u<<i))) {
   int64_t uw=(int64_t)s->voltage_mv[i]*s->current_ma[i];
   put(&t,"\"voltage_mv\":%u,\"current_ma\":%d,\"power_mw\":%" PRId64 "}",s->voltage_mv[i],s->current_ma[i],uw/1000);
   total_uw+=uw;
   valid++;
  } else put(&t,"\"voltage_mv\":null,\"current_ma\":null,\"power_mw\":null}");
 }
 put(&t,"],\"valid_slots\":%u,\"complete\":%s,\"total_mw\":",valid,boolean(valid==12));
 if(valid)put(&t,"%" PRId64,total_uw/1000);else put(&t,"null");
 put(&t,"}\n");
 return t.error?t.error:(int)t.used;
}
