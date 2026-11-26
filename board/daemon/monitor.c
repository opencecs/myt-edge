/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include "board_daemon.h"
#include "vendor/y4_os_mcu.h"
#include "vendor/y4_os_cooling.h"
#include "vendor/y4_os_mcu_health.h"
#include "vendor/y4_os_mcu_backend.h"
#include "vendor/y4_os_slots.h"
#include "vendor/y4_os_disk_activity.h"
#include "vendor/y4_os_fan_policy.h"
#include "vendor/y4_os_fan1_tach.h"
#include <errno.h>
#include <glob.h>
#include <limits.h>
#include <linux/gpio.h>
#include <sys/ioctl.h>
#include <stdlib.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/file.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
extern int y4_board_display_publish(int,const struct y4_board_sample *);
extern int y4_board_pcie_temperature(struct y4_board_temperature *);
static void stop(int sig){(void)sig;y4_board_stopping=1;}
static uint64_t clock_now(void *unused){(void)unused;return y4_board_now_ms();}
static int cancelled(void *unused){(void)unused;return y4_board_stopping;}
/* Publish the chassis sensor on V11 without writing any fan controls. */
static int mcu_inside_display(int fd,const struct y4_board_temperature *inside)
{
 int rc=y4_mcu_write_verified(fd,0x48,0);
 if(rc||!inside->valid||inside->millicelsius < -40000||inside->millicelsius > 125000)return rc;
 uint16_t t=(uint16_t)(int16_t)(inside->millicelsius/100);
 rc=y4_mcu_write_verified(fd,0x46,(uint8_t)t);
 if(!rc)rc=y4_mcu_write_verified(fd,0x47,(uint8_t)(t>>8));
 if(!rc)rc=y4_mcu_write_verified(fd,0x48,1);
 return rc;
}

static int mcu_owns_fan_duty(const struct y4_mcu_identity *id) {
 return id->version[0]==0u&&id->version[1]==1u&&id->version[2]>=43u;
}
static int configure(int fd,const struct y4_mcu_identity *id) {
 if(!mcu_owns_fan_duty(id)) {
  for(uint8_t i=0;i<4u;i++){
   int e=y4_mcu_write_verified(fd,(uint8_t)(1u+i),100);
   if(e)return e;
  }
 }
 const uint8_t values[][2]={{0x43,100},{0x4b,10},{0x48,0},{0x49,Y4_INSTALLED_FAN_MASK},
 {0x4a,Y4_INSTALLED_FAN_MASK},{0x52,2},{0x4c,100},{0x4d,0},{0x00,1}};
 for(unsigned i=0;i<sizeof values/sizeof values[0];i++){int e=y4_mcu_write_verified(fd,values[i][0],values[i][1]);if(e)return e;}
 if(id->capabilities&8u){int e=y4_mcu_write_verified(fd,0x44,1);if(e)return e;return y4_mcu_write_verified(fd,0x45,0);}
 return 0;
}
static int lock_open(void) {
 int fd=open(Y4_BOARD_RUNTIME "/mcu.lock",O_RDWR|O_CREAT|O_NOFOLLOW|O_CLOEXEC,0600);if(fd<0)return -errno;
 struct stat st;int e=fstat(fd,&st)?-errno:0;
 if(!e&&(!S_ISREG(st.st_mode)||st.st_uid||st.st_nlink!=1u||(st.st_mode&07777u)!=0600u))e=-EPERM;
 if(e){close(fd);return e;}return fd;
}
/* The protection registers the host owns. A silent MCU reset between samples
 * returns them to zero while the UID stays identical, so an unchanged UID is
 * never accepted as proof that the fan protection set survived. */
static int protection_matches(int fd) {
 uint8_t fan=0,tach=0,timeout=0,ppr=0,min[2]={0,0};
 if(y4_mcu_read(fd,0x49,&fan,1)||y4_mcu_read(fd,0x4a,&tach,1)||
    y4_mcu_read(fd,0x4b,&timeout,1)||y4_mcu_read(fd,0x52,&ppr,1)||
    y4_mcu_read(fd,0x4c,min,2))return -1;
 return fan==Y4_INSTALLED_FAN_MASK&&tach==Y4_INSTALLED_FAN_MASK&&timeout==10&&
  ppr==2&&(min[0]|((unsigned)min[1]<<8))==100;
}
static int startup_complete(void) {
 char status[8192],current[64],recorded[40];size_t bytes=0;long long schema=0;int complete=0;
 int e=y4_board_read_file(Y4_BOARD_INIT_STATUS,status,sizeof status,NULL);if(e)return e;
 if(y4_board_json_number(status,"schema",&schema)||schema!=1||
    y4_board_json_bool(status,"completed",&complete)||!complete)return -EINPROGRESS;
 if(y4_board_json_string(status,"boot_id",recorded,sizeof recorded))return -EPROTO;
 e=y4_board_read_file("/proc/sys/kernel/random/boot_id",current,sizeof current,&bytes);if(e)return e;
 while(bytes&&(current[bytes-1u]=='\n'||current[bytes-1u]=='\r'))current[--bytes]=0;
 if(bytes!=36u||strcmp(current,recorded))return -ESTALE;
 int lock=open(Y4_BOARD_RUNTIME "/boot.lock",O_RDONLY|O_NOFOLLOW|O_CLOEXEC);if(lock<0)return -errno;
 struct stat st;e=fstat(lock,&st)?-errno:0;
 if(!e&&(!S_ISREG(st.st_mode)||st.st_uid||st.st_nlink!=1u||(st.st_mode&07777u)!=0600u))e=-EPERM;
 if(!e&&flock(lock,LOCK_EX|LOCK_NB))e=errno==EAGAIN||errno==EWOULDBLOCK?-EINPROGRESS:-errno;
 close(lock);return e;
}
/* Named input lines are scoped to Y12 and independent of chip enumeration. */
static void y12_read_presence(struct y4_board_sample *sample)
{
 glob_t chips={0};
 uint16_t seen=0;
 if(glob("/dev/gpiochip*",0,NULL,&chips))goto out;
 for(size_t i=0;i<chips.gl_pathc;i++) {
  int chip=open(chips.gl_pathv[i],O_RDONLY|O_CLOEXEC|O_NOFOLLOW);
  if(chip<0)continue;
  struct gpiochip_info info={0};
  if(ioctl(chip,GPIO_GET_CHIPINFO_IOCTL,&info)<0){close(chip);continue;}
  for(unsigned offset=0;offset<info.lines;offset++) {
   struct gpioline_info line={.line_offset=offset};
   if(ioctl(chip,GPIO_GET_LINEINFO_IOCTL,&line)<0)continue;
   unsigned slot;
   int end=0;
   if(sscanf(line.name,"y12-slot%u-present%n",&slot,&end)!=1||
      !end||line.name[end]||slot<1||slot>12)continue;
   uint16_t bit=1u<<(slot-1);
   if(seen&bit){sample->y12_present_valid&=~bit;continue;}
   seen|=bit;
   struct gpiohandle_request request={
    .lineoffsets={offset},.consumer_label="y12-slot-presence",.lines=1,
    .flags=GPIOHANDLE_REQUEST_INPUT|GPIOHANDLE_REQUEST_ACTIVE_LOW,
   };
   if(ioctl(chip,GPIO_GET_LINEHANDLE_IOCTL,&request)<0)continue;
   struct gpiohandle_data value={0};
   if(ioctl(request.fd,GPIOHANDLE_GET_LINE_VALUES_IOCTL,&value)==0) {
    sample->y12_present_valid|=bit;
    if(value.values[0])sample->y12_present|=bit;
   }
   close(request.fd);
  }
  close(chip);
 }
out:
 globfree(&chips);
}

static int y12_slot_online(uint16_t *online)
{
 glob_t roots={0}, devices={0};
 int error=-ENODEV;
 uint16_t mask=0;
 int switch_found=0;
 if(glob("/sys/bus/platform/devices/*/of_node/myt,slot-downstream-ports",0,NULL,&roots) ||
    roots.gl_pathc!=1)goto out;
 char tree[PATH_MAX];
 if(!realpath(roots.gl_pathv[0],tree)){error=-errno;goto out;}
 uint8_t ports[48];
 FILE *file=fopen(tree,"rb");
 if(!file){error=-errno;goto out;}
 size_t count=fread(ports,1,sizeof ports,file);
 int extra=fgetc(file);
 fclose(file);
 if(count!=sizeof ports||extra!=EOF){error=-EPROTO;goto out;}
 unsigned seen=0;
 for(unsigned i=0;i<12;i++) {
  unsigned n=ports[4*i+3];
  if(ports[4*i]||ports[4*i+1]||ports[4*i+2]||n>=32||(seen&(1u<<n))){
   error=-EPROTO;goto out;
  }
  seen|=1u<<n;
 }
 *strrchr(tree,'/')=0;
 int rc=glob("/sys/bus/pci/devices/*/class",0,NULL,&devices);
 if(rc && rc!=GLOB_NOMATCH){error=-EIO;goto out;}
 for(size_t i=0;i<devices.gl_pathc;i++) {
  char path[PATH_MAX];
  if(!realpath(devices.gl_pathv[i],path))continue;
  file=fopen(path,"r");
  if(!file)continue;
  unsigned cls;
  int valid=fscanf(file,"%x",&cls)==1;
  fclose(file);
  if(!valid)continue;
  /* Walk toward the host bridge, retaining the root port and first two
   * switch levels. A card can contain bridges of its own. */
  char *base=strrchr(path,'/');
  *base=0;
  int ix8024=0;
  if(cls==0x060400) {
   char id_path[PATH_MAX];
   unsigned id[2];
   const char *const names[]={"vendor","device"};
   unsigned n_id;
   for(n_id=0;n_id<2;n_id++) {
    int n=snprintf(id_path,sizeof id_path,"%s/%s",path,names[n_id]);
    if(n<0||(size_t)n>=sizeof id_path)break;
    file=fopen(id_path,"r");
    if(!file)break;
    valid=fscanf(file,"%x",&id[n_id])==1;
    fclose(file);
    if(!valid)break;
   }
   ix8024=n_id==2&&id[0]==0x1ec8&&id[1]==0x8024;
  }
  unsigned devnos[3]={0}, depth=0;
  while((base=strrchr(path,'/'))) {
   unsigned domain,bus,dev,fn;
   char tail;
   if(sscanf(base+1,"%x:%x:%x.%x%c",&domain,&bus,&dev,&fn,&tail)!=4)break;
   devnos[2]=devnos[1];
   devnos[1]=devnos[0];
   devnos[0]=dev;
   depth++;
   *base=0;
  }
  if(depth<4&&!(depth==2&&ix8024))continue;
  char node[PATH_MAX], resolved[PATH_MAX];
  int n=snprintf(node,sizeof node,"%s/../of_node",path);
  if(n<0||(size_t)n>=sizeof node||!realpath(node,resolved)||strcmp(tree,resolved))continue;
  if(depth==2){switch_found=1;continue;}
  if((cls>>16)==6)continue;
  for(unsigned slot=0;slot<12;slot++)
   if(devnos[2]==ports[4*slot+3])mask|=1u<<slot;
 }
 if(!switch_found)goto out;
 *online=mask;
 error=0;
out:
 globfree(&devices);
 globfree(&roots);
 return error;
}

/* The kernel owns the U69 GPIOs; this worker uses the LED class only. */
static void y12_sync_slot_leds(struct y4_board_sample *s)
{
 for(unsigned i=0;i<12;i++) {
  char path[128];
  snprintf(path,sizeof path,"/sys/class/leds/slot%u:run/brightness",i+1);
  int fd=open(path,O_RDWR|O_CLOEXEC);
  if(fd<0)continue;
  char current[16];
  ssize_t len=pread(fd,current,sizeof current,0);
  if(len>0&&(current[0]=='0'||(current[0]>='1'&&current[0]<='9'))) {
   char desired=s->y12_slot_online&(1u<<i)?'1':'0';
   if(!s->slots_error && (current[0]!='0')!=(desired=='1')) {
    char request[]={desired,'\n'};
    if(write(fd,request,sizeof request)!=(ssize_t)sizeof request ||
       pread(fd,current,sizeof current,0)<=0) {
     close(fd);
     continue;
    }
   }
   s->slot_led_valid|=1u<<i;
   if(current[0]!='0')s->slot_led_on|=1u<<i;
  }
  close(fd);
 }
}

static unsigned power_le16(const uint8_t *p)
{
 return p[0] | ((unsigned)p[1]<<8);
}

static int y12_read_power(int fd,struct y4_board_sample *s)
{
 struct y4_mcu_identity identity;
 uint8_t frame[92];
 int err=y12_mcu_identify(fd,&identity,s->uid);
 if(err)return err;
 err=y4_mcu_read(fd,0,frame,sizeof frame);
 if(err)return err;
 if(memcmp(frame,"Y12P",4)||frame[4]!=2||frame[5]!=12)return -EPROTONOSUPPORT;
 s->uid_valid=1;
 memcpy(s->firmware,identity.version,sizeof s->firmware);
 s->power_sequence=power_le16(frame+6);
 for(unsigned i=0;i<12;i++) {
  const uint8_t *entry=frame+8+7*i;
  if(entry[0]&2) {
   unsigned temp=power_le16(entry+5);
   s->temperature_dc[i]=temp>=32768?(int)temp-65536:(int)temp;
   s->temperature_valid|=1u<<i;
  }
  if(!(entry[0]&1))continue;
  unsigned current=power_le16(entry+3);
  s->voltage_mv[i]=power_le16(entry+1);
  s->current_ma[i]=current>=32768?(int)current-65536:(int)current;
  s->power_valid|=1u<<i;
 }
 return 0;
}

int y4_board_monitor_run(int channel) {
 struct sigaction sa={.sa_handler=stop};sigemptyset(&sa.sa_mask);
 if(sigaction(SIGTERM,&sa,NULL)||sigaction(SIGINT,&sa,NULL))return 1;
 struct y4_cooling cooling={.duty=100};struct y4_disk_activity activity={0};
 struct y4_mcu_backend_tracker backend={0};int configured=0,restore=0,uptime_known=0;
 uint8_t configured_uid[12]={0},heartbeat=0,previous_online=0;uint32_t previous_uptime=0;
 uint16_t power_sequence=0;
 uint64_t power_changed_ms=0;
 while(!y4_board_stopping) {
  struct y4_board_sample s={.magic=Y4_BOARD_SAMPLE_MAGIC,.sampled_ms=y4_board_now_ms(),
   .mcu_error=-EINPROGRESS,.fan_error=-ENODATA,.health_error=-ENODATA,.backend_error=-ENODATA,.display_error=-ENODATA,
   .fan_policy_error=-ENODATA,.fan1_error=-ENODATA,.fan2_error=-ENODATA};
  if(!s.sampled_ms)return 1;
  struct y4_temperature_context context={"/sys",1};
  struct y4_call call={.opaque=&context,.now_ms=clock_now,.cancelled=cancelled,
   .started_ms=s.sampled_ms,.deadline_ms=s.sampled_ms+500u};
  struct y4_temperature_snapshot thermal={0};s.temperature_error=y4_temperature_read(&call,&thermal);
  s.cpu=(struct y4_board_temperature){!s.temperature_error&&thermal.soc_valid&&!thermal.soc_error,
   s.temperature_error?s.temperature_error:thermal.soc_error,thermal.soc_max,0u};
  s.inside=(struct y4_board_temperature){!s.temperature_error&&thermal.pm_checked&&!thermal.pm_error,
   s.temperature_error?s.temperature_error:thermal.pm_checked?thermal.pm_error:-ENODATA,thermal.pm.millicelsius,0u};
  (void)y4_board_pcie_temperature(&s.pcie_switch);
  struct y4_cooling_output control=y4_cooling_step(&cooling,s.temperature_error,&thermal,thermal.pm_checked&&!thermal.pm_error);
  s.cooling_error=control.error;s.requested_pwm=(uint8_t)control.duty;
  struct y4_slots slots={0};s.slots_error=y4_slots_read("/sys/bus/pci/devices",&slots);
  if(!s.slots_error){s.slot_online=slots.online;memcpy(s.slot_type,slots.type,sizeof s.slot_type);}
  struct y4_disk_counters counters={0};s.activity_error=y4_disk_counters_read("/sys/class/block",&counters);
  s.slot_active=y4_disk_activity_step(&activity,s.activity_error?NULL:&counters);
  int net_status=y4_network_sample(&s.network);
  /* FAN1 tach is independent of the MCU bus: kernel pwm-fan hwmon sysfs. */
  s.fan1_error=y4_soc_fan_tach_read(1,&s.fan1_valid,&s.fan1_rpm);
  s.fan2_error=y4_soc_fan_tach_read(2,&s.fan2_valid,&s.fan2_rpm);
  /* The kernel cannot prove an ongoing DHCP exchange; the manager state is
   * read separately and stays unknown when it is not observable. */
  (void)y4_board_nm_states(s.nm);
  if(net_status){s.network.error=net_status;s.network.default_egress=Y4_NET_EGRESS_NONE;
   for(unsigned i=0;i<2u;i++){s.network.logical[i].ipv4_valid=0;s.network.logical[i].ipv4[0]=0;}}
  if(!strcmp(Y4_DEVICE_ID,"Y12")) {
   y12_board_fans_sample(&s);
   y12_read_presence(&s);
   s.slots_error=y12_slot_online(&s.y12_slot_online);
   y12_sync_slot_leds(&s);
   int lock=lock_open();
   if(lock<0)s.power_error=lock;
   else if(flock(lock,LOCK_EX|LOCK_NB)) {
    s.power_error=-errno;
    s.maintenance=errno==EAGAIN||errno==EWOULDBLOCK;
   } else {
    int fd=open("/dev/i2c-1",O_RDWR|O_CLOEXEC|O_NOFOLLOW);
    s.power_error=fd<0?-errno:y12_read_power(fd,&s);
    if(fd>=0)close(fd);
   }
   if(lock>=0)close(lock);
   if(!s.power_error) {
    if(!power_changed_ms||s.power_sequence!=power_sequence) {
     power_sequence=s.power_sequence;
     power_changed_ms=s.sampled_ms;
    } else if(s.sampled_ms-power_changed_ms>=3000u) {
     s.power_error=-ESTALE;
    }
   }
   s.mcu_error=s.power_error;
   goto publish;
  }
  int lock=-1,fd=-1,held=0,initialization=startup_complete();
  if(initialization){s.mcu_error=initialization;s.maintenance=1;}
  else {
   lock=lock_open();
   if(lock<0)s.mcu_error=lock;
   else if(flock(lock,LOCK_EX|LOCK_NB)){s.mcu_error=-errno;s.maintenance=errno==EAGAIN||errno==EWOULDBLOCK;}
   else {held=1;fd=open("/dev/i2c-1",O_RDWR|O_CLOEXEC|O_NOFOLLOW);s.mcu_error=fd<0?-errno:0;}
  }
  struct y4_mcu_identity id={0};int identified=0;
  if(!s.mcu_error){s.mcu_error=y4_mcu_identify(fd,&id);identified=!s.mcu_error;}
  if(!s.mcu_error) {
   uint8_t uid[12];int e=y4_mcu_factory_uid(fd,uid);
   if(!e){memcpy(s.uid,uid,12);s.uid_valid=1;if(memcmp(configured_uid,uid,12)){configured=0;memcpy(configured_uid,uid,12);}}
   else s.mcu_error=e;
  }
  if(!s.mcu_error) {
   uint8_t up[4];int e=y4_mcu_read(fd,0x3a,up,4);
   if(e)s.mcu_error=e;
   else {
    uint32_t uptime=(uint32_t)(up[0]|((uint32_t)up[1]<<8)|((uint32_t)up[2]<<16)|((uint32_t)up[3]<<24));
    /* Backwards uptime with the same UID: the MCU rebooted between samples. */
    if(configured&&uptime_known&&uptime<previous_uptime){configured=0;restore=1;}
    previous_uptime=uptime;uptime_known=1;
   }
  }
  if(!s.mcu_error&&configured) {
   int match=protection_matches(fd);
   if(match<0)s.mcu_error=match;else if(!match){configured=0;restore=1;}
  }
  if(!s.mcu_error&&!configured&&restore) {
   /* Lost protection: hold safe full speed for this cycle, reconfigure
    * under the resource lock, and verify before reduced duty returns. */
   control.duty=100;cooling=(struct y4_cooling){.duty=100};
   s.mcu_error=configure(fd,&id);
   if(!s.mcu_error){int match=protection_matches(fd);
    if(match<0)s.mcu_error=match;else if(!match)s.mcu_error=-EIO;
    else {configured=1;restore=0;}}
  }
  else if(!s.mcu_error&&!configured){s.mcu_error=configure(fd,&id);if(!s.mcu_error)configured=1;}
  if(!s.mcu_error) {
   memcpy(s.firmware,id.version,3);
   /* V11 MCU owns duty. Keep publishing temperatures for display and
    * protocol compatibility, without requesting a temperature-based speed. */
   int owns_duty=mcu_owns_fan_duty(&id);
   if(owns_duty)s.requested_pwm=20u;
   if(!s.mcu_error&&id.version[0]==0u&&id.version[1]==1u&&id.version[2]>=19u)
    s.mcu_error=y4_mcu_soc_display(fd,s.temperature_error,&thermal);
   if(!s.mcu_error)s.mcu_error=y4_mcu_write_verified(fd,0x09,s.slot_online);
   if(!s.mcu_error)s.mcu_error=y4_mcu_write_verified(fd,0x40,s.slot_active);
   for(unsigned i=0;!s.mcu_error&&i<4u;i++) {
    s.mcu_error=y4_mcu_write_verified(fd,(uint8_t)(0x78u+i),s.slot_type[i]);
    if(!s.mcu_error) {
     /* Enumeration absence alone never proves a verified empty slot: with
      * prior online evidence the device is LOST, without it the state is
      * UNKNOWN. A power hold, training failure or fault must not be
      * displayed as confirmed EMPTY. */
     unsigned state=s.slots_error?0u:(s.slot_online&(1u<<i))?2u:(previous_online&(1u<<i))?4u:0u;
     s.mcu_error=y4_mcu_write_verified(fd,(uint8_t)(0x74u+i),(uint8_t)state);
    }
   }
   if(!s.slots_error)previous_online=(uint8_t)(previous_online|s.slot_online);
   if(!s.mcu_error)s.mcu_error=y4_mcu_write_verified(fd,0x05,++heartbeat);
   if(!s.mcu_error)s.display_error=y4_board_display_publish(fd,&s);
   if(!s.mcu_error) {
    if(owns_duty)s.mcu_error=mcu_inside_display(fd,&s.inside);
    else s.mcu_error=y4_mcu_cooling_apply_for(fd,&id,&control);
   }
   struct y4_mcu_fans fans={0};s.fan_error=y4_mcu_fans_read(fd,&id,&fans);
   if(!s.fan_error){memcpy(s.rpm,fans.rpm,sizeof s.rpm);memcpy(s.effective_pwm,fans.effective,sizeof s.effective_pwm);
    s.fault_bits=fans.fault;s.fan_mask=fans.fan_mask;s.tach_mask=fans.tach_mask;}
   struct y4_fan_policy_state policy={0};
   s.fan_policy_error=y4_fan_policy_fetch(fd,&id,&policy);
   if(!s.fan_policy_error){s.fan_policy_mode=policy.mode;s.fan_policy_status=policy.status;
    memcpy(s.fan_policy_duty,policy.duty,sizeof policy.duty);}
   struct y4_mcu_health health={0};s.health_error=y4_mcu_health_read(fd,&health);
   if(!s.health_error){s.reset_flags=health.reset_flags;s.fault_count=health.fault_count;
    s.communication_errors=health.communication_errors;s.minimum_stack_pointer=health.minimum_stack_pointer;}
   struct y4_mcu_backend_frame frame={0};int e=y4_mcu_backend_observe(fd,&id,&frame);
   struct y4_mcu_backend_snapshot observed={0};s.backend_error=y4_mcu_backend_track(&backend,y4_board_now_ms(),e,&frame,&observed);
   if(!s.backend_error)s.backend_error=observed.status;
   if(!s.backend_error){s.pwm_hz=observed.frame.pwm_hz;s.spi_hz=observed.frame.spi_hz;
    s.dma_completed=observed.frame.dma_completed;s.backend_error_events=observed.frame.error_events;}
  }
  if(s.mcu_error){configured=0;restore=1;cooling=(struct y4_cooling){.duty=100};backend=(struct y4_mcu_backend_tracker){0};
   if(held&&fd>=0&&identified)(void)y4_mcu_write_verified(fd,0x48,0);}
  if(fd>=0)close(fd);
  if(lock>=0)close(lock); /* Closing only our flock releases bus ownership. */
publish:
  s.completed_ms=y4_board_now_ms();
  ssize_t sent=send(channel,&s,sizeof s,MSG_DONTWAIT|MSG_NOSIGNAL);
  if(sent<0&&errno!=EAGAIN&&errno!=EWOULDBLOCK&&errno!=EINTR)break;
  uint64_t next=s.sampled_ms+1000u,now=y4_board_now_ms();
  if(now&&now<next){uint64_t delay=next-now;struct timespec pause={(time_t)(delay/1000u),(long)(delay%1000u)*1000000L};
   while(!y4_board_stopping&&nanosleep(&pause,&pause)&&errno==EINTR){}}
 }
 close(channel);
 return 0;
}
