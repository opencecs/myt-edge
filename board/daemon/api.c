/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include "board_daemon.h"
#include "ssh_api.h"
#include "resources.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <openssl/evp.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/file.h>
#include <sys/prctl.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include "vendor/y4_os_fan_policy.h"
#define CLIENTS 8u
#define EVENT_CLIENTS 2u
#define RESOURCE_INTERVAL_MS 1000u
#define EVENT_WRITE_TIMEOUT_MS 5000u
#define EVENT_DATA_CAP 4096u
#define HEADER_BYTES 4096u
#define RESPONSE_BYTES 24576u
#define UPLOAD_TIME_MS 120000u
#define OTA_UPLOAD_TIME_MS 3600000u
#define UPLOAD_IDLE_MS 30000u
#define HELPER_OVERDUE_MS 600000u
#define FANS_BODY_LIMIT 512u
#define SSH_REPLY_TIMEOUT_MS 60000u
struct client {
 int fd,upload_fd,parsed,response,upload_reserved,ota,fans_post,subscribe;
 char header[HEADER_BYTES+1],output[RESPONSE_BYTES];
 size_t header_bytes,out_bytes,out_sent,expected,received;
 uint64_t deadline,upload_idle_deadline;
 EVP_MD_CTX *digest;
 char job[33],name[64],path[PATH_MAX];
 char fans_body[FANS_BODY_LIMIT+1u];
 enum y4_ssh_operation ssh_operation;
 uint64_t ssh_request;
 char ssh_body[Y4_SSH_BODY_LIMIT+1u];
};
enum event_topic {
 EVENT_RESOURCES,EVENT_FANS,EVENT_TEMPERATURE,EVENT_SLOT_POWER,EVENT_OTA,EVENT_MCU,EVENT_TOPIC_COUNT
};
static const char *const event_names[EVENT_TOPIC_COUNT]={
 [EVENT_RESOURCES]="system-resources",
 [EVENT_FANS]="fan-status",
 [EVENT_TEMPERATURE]="temperature",
 [EVENT_SLOT_POWER]="slot-power",
 [EVENT_OTA]="ota-status",
 [EVENT_MCU]="mcu-update",
};
struct event_value {
 uint64_t generation;
 size_t bytes;
 char data[EVENT_DATA_CAP];
};
struct event_client {
 int fd;
 size_t bytes,sent;
 uint64_t deadline,seen[EVENT_TOPIC_COUNT];
 unsigned next_topic;
 char output[EVENT_DATA_CAP+256u];
};
struct update_job {
 pid_t pid;
 int code,verified,overdue,cancel_requested,blocked;
 uint64_t started_ms,completed_ms,sequence;
 char id[33],name[64],path[PATH_MAX],sha256[65],phase[40],version[32],uid[25];
 char status_path[PATH_MAX],log_path[PATH_MAX];
};
static int upload_busy;
static uint64_t ssh_sequence;
static dev_t socket_device;
static ino_t socket_inode;
static int socket_owned;
static struct update_job job;
static struct y4_board_sample sample;
static struct y4_resource_sampler resource_sampler;
static struct event_value event_values[EVENT_TOPIC_COUNT];
static uint64_t resource_next,event_next,telemetry_sampled_ms,mcu_event_started_ms,ota_phase_generation;
static int telemetry_fresh,fan_event_dirty,ota_event_phase=-1;
static char mcu_event_phase[sizeof job.phase];
static void stop(int sig){(void)sig;y4_board_stopping=1;}
static int valid_hex(const char *s,size_t n) {
 if(strlen(s)!=n)return 0;
 for(size_t i=0;i<n;i++)if(!((s[i]>='0'&&s[i]<='9')||(s[i]>='a'&&s[i]<='f')))return 0;
 return 1;
}
static int valid_word(const char*s) {
 if(!*s)return 0;
 for(;*s;s++)if(!((*s>='a'&&*s<='z')||(*s>='A'&&*s<='Z')||(*s>='0'&&*s<='9')||*s=='_'||*s=='-'||*s=='.'||*s=='+'))return 0;
 return 1;
}
static int version_valid(const char *text) {
 unsigned a,b,c;char extra;return sscanf(text,"%u.%u.%u%c",&a,&b,&c,&extra)==3&&a<=255u&&b<=255u&&c<=255u;
}
static int uid_valid(const char *text) {
 return valid_hex(text,24u)&&strcmp(text,"000000000000000000000000")&&strcmp(text,"ffffffffffffffffffffffff");
}
static int path_format(char*out,size_t cap,const char*dir,const char*name) {
 int n=snprintf(out,cap,"%s/%s",dir,name);return n<0||(size_t)n>=cap?-ENAMETOOLONG:0;
}
static int secure_lock(const char *name) {
 char path[PATH_MAX];if(path_format(path,sizeof path,Y4_BOARD_RUNTIME,name))return -EINVAL;
 int fd=open(path,O_RDWR|O_CREAT|O_NOFOLLOW|O_CLOEXEC,0600);if(fd<0)return -errno;
 struct stat st;int e=fstat(fd,&st)?-errno:0;
 if(!e&&(!S_ISREG(st.st_mode)||st.st_uid||st.st_nlink!=1u||(st.st_mode&07777u)!=0600u))e=-EPERM;
 if(!e&&flock(fd,LOCK_EX|LOCK_NB))e=-errno;
 if(e){close(fd);return e;}return fd;
}
static int job_save(void) {
 char json[1800];
 int n=snprintf(json,sizeof json,
 "{\"schema\":1,\"job\":\"%s\",\"phase\":\"%s\",\"status\":%d,\"verified\":%s,"
 "\"overdue\":%s,\"cancel_requested\":%s,\"blocked\":%s,\"started_ms\":%" PRIu64 ","
 "\"completed_ms\":%" PRIu64 ",\"sequence\":%" PRIu64 ",\"package\":\"%s\",\"sha256\":\"%s\",\"version\":\"%s\",\"uid\":\"%s\"}\n",
 job.id,job.phase,job.code,job.verified?"true":"false",job.overdue?"true":"false",
 job.cancel_requested?"true":"false",job.blocked?"true":"false",job.started_ms,job.completed_ms,job.sequence,
 job.name,job.sha256,job.version,job.uid);
 if(n>0&&(size_t)n<sizeof json) {
  int e=y4_board_atomic_file(Y4_BOARD_STATE,"update.json",json,(size_t)n,0600);
  if(e)fprintf(stderr,"y4-board update receipt persistence failed status=%d\n",e);
  return e;
 }
 return -EOVERFLOW;
}
static int job_restore(void) {
 char text[4096];int e=y4_board_read_file(Y4_BOARD_STATE "/update.json",text,sizeof text,NULL);
 if(e==-ENOENT){strcpy(job.phase,"idle");return 0;}if(e)return e;
 long long schema,status;
 if(y4_board_json_number(text,"schema",&schema)||schema!=1||
 y4_board_json_string(text,"job",job.id,sizeof job.id)||!valid_hex(job.id,32u)||
 y4_board_json_string(text,"phase",job.phase,sizeof job.phase)||!valid_word(job.phase)||
 y4_board_json_number(text,"status",&status)||status>0||status< -4095)return -EBADMSG;
 job.code=(int)status;
 (void)y4_board_json_bool(text,"verified",&job.verified);
 (void)y4_board_json_unsigned(text,"sequence",&job.sequence);
 if(y4_board_json_string(text,"package",job.name,sizeof job.name)||strchr(job.name,'/')||
 strncmp(job.name,"firmware-",9)||strlen(job.name)!=45u||strcmp(job.name+41,".bin"))return -EBADMSG;
 if(y4_board_json_string(text,"sha256",job.sha256,sizeof job.sha256)||!valid_hex(job.sha256,64u))return -EBADMSG;
 (void)y4_board_json_string(text,"version",job.version,sizeof job.version);
 (void)y4_board_json_string(text,"uid",job.uid,sizeof job.uid);
 if(!valid_word(job.version)&&*job.version)return -EBADMSG;
 if(*job.uid&&!valid_hex(job.uid,24u))return -EBADMSG;
 if(path_format(job.path,sizeof job.path,Y4_BOARD_STATE,job.name))return -ENAMETOOLONG;
 char name[80];(void)snprintf(name,sizeof name,"update-%s.json",job.id);
 if(path_format(job.status_path,sizeof job.status_path,Y4_BOARD_RUNTIME,name))return -ENAMETOOLONG;
 if(!strcmp(job.phase,"accepted")||!strcmp(job.phase,"running")||!strcmp(job.phase,"cancelling")||
    !strcmp(job.phase,"overdue")||!strcmp(job.phase,"interrupted_unknown")) {
  strcpy(job.phase,"interrupted_unknown");job.code=-EINPROGRESS;job.blocked=1;job_save();
 }
 return 0;
}
static int helper_result(void) {
 char text[8192],phase[40];int e=y4_board_read_file(job.status_path,text,sizeof text,NULL);if(e)return e;
 long long schema=0,status=0;int completed=0,verified=0,safe=0;
 if(y4_board_json_number(text,"schema",&schema)||schema!=1||
 y4_board_json_number(text,"status",&status)||status>0||status< -4095||
 y4_board_json_bool(text,"completed",&completed)||!completed||
 y4_board_json_bool(text,"verified",&verified)||!verified||
 y4_board_json_bool(text,"safe_for_network",&safe)||!safe||status)return status<0?(int)status:-EPROTO;
 if(y4_board_json_unsigned(text,"sequence",&job.sequence)||
    y4_board_json_string(text,"uid",job.uid,sizeof job.uid)||!uid_valid(job.uid)||
    y4_board_json_string(text,"version",job.version,sizeof job.version)||!version_valid(job.version))return -EPROTO;
 if(y4_board_json_string(text,"phase",phase,sizeof phase)||(strcmp(phase,"complete")&&strcmp(phase,"kept")))return -EPROTO;
 return 0;
}
static int active_publish(void) {
 char text[1024];int n=snprintf(text,sizeof text,
 "{\"schema\":1,\"package\":\"%s\",\"sha256\":\"%s\",\"job\":\"%s\",\"version\":\"%s\",\"uid\":\"%s\",\"sequence\":%" PRIu64 "}\n",
 job.name,job.sha256,job.id,job.version,job.uid,job.sequence);
 if(n<0||(size_t)n>=sizeof text)return -EOVERFLOW;
 return y4_board_atomic_file(Y4_BOARD_STATE,"active.json",text,(size_t)n,0600);
}
static void job_tick(void) {
 if(job.pid<=0)return;
 int status=0;pid_t got=waitpid(job.pid,&status,WNOHANG);
 if(!got) {
  uint64_t now=y4_board_now_ms();
  if(!job.overdue&&now>=job.started_ms&&now-job.started_ms>=HELPER_OVERDUE_MS) {
   job.overdue=1;strcpy(job.phase,"overdue");job.code=-EINPROGRESS;job_save();
   fprintf(stderr,"y4-board MCU updater overdue pid=%ld; ownership retained\n",(long)job.pid);
  }
  return;
 }
 if(got<0) {
  if(errno==EINTR)return;
  job.blocked=1;job.code=-errno;job.pid=0;strcpy(job.phase,"interrupted_unknown");(void)job_save();return;
 }
 job.pid=0;job.completed_ms=y4_board_now_ms();
 int e=WIFEXITED(status)&&WEXITSTATUS(status)==0?helper_result():-EIO;
 if(!e){job.verified=1;e=active_publish();strcpy(job.phase,e?"verified_unpublished":"complete");}
 else strcpy(job.phase,job.cancel_requested?"cancelled":"failed");
 job.code=e;job_save();
}
static int host_version(char out[64]) {
 size_t n;int e=y4_board_read_file(Y4_BOARD_HOST_VERSION,out,64,&n);if(e)return e;
 while(n&&(out[n-1]=='\n'||out[n-1]=='\r'))out[--n]=0;
 return valid_word(out)?0:-EBADMSG;
}
static int launch_helper(void) {
 char version[64],floor_text[32];int e=host_version(version);if(e)return e;
 char active[2048],builtin[2048],selected_sha[65];uint64_t floor=0;long long schema=0;
 e=y4_board_read_file("/usr/share/y4-board/mcu-builtin.json",builtin,sizeof builtin,NULL);if(e)return e;
 if((e=y4_board_json_number(builtin,"schema",&schema))||schema!=1||
    (e=y4_board_json_unsigned(builtin,"sequence",&floor))||
    (e=y4_board_json_string(builtin,"sha256",selected_sha,sizeof selected_sha))||!valid_hex(selected_sha,64u))return e?e:-EBADMSG;
 e=y4_board_read_file(Y4_BOARD_STATE "/active.json",active,sizeof active,NULL);
 if(e&&e!=-ENOENT)return e;
 if(!e) {
  char prior_sha[65];uint64_t prior_sequence=0;
  if((e=y4_board_json_number(active,"schema",&schema))||schema!=1||
    (e=y4_board_json_unsigned(active,"sequence",&prior_sequence))||
    (e=y4_board_json_string(active,"sha256",prior_sha,sizeof prior_sha))||!valid_hex(prior_sha,64u))return e?e:-EBADMSG;
  if(prior_sequence==floor&&strcmp(prior_sha,selected_sha))return -EBADMSG;
  if(prior_sequence>=floor){floor=prior_sequence;memcpy(selected_sha,prior_sha,sizeof selected_sha);}
 }
 if(strcmp(selected_sha,job.sha256)){if(floor==UINT64_MAX)return -EOVERFLOW;++floor;}
 (void)snprintf(floor_text,sizeof floor_text,"%" PRIu64,floor);
 struct stat st;
 if(lstat(Y4_BOARD_MAINTAIN,&st))return -errno;
 if(!S_ISREG(st.st_mode)||st.st_uid||(st.st_mode&0022u)||!(st.st_mode&0111u))return -EPERM;
 char status_name[80],log_name[80];
 (void)snprintf(status_name,sizeof status_name,"update-%s.json",job.id);
 (void)snprintf(log_name,sizeof log_name,"firmware-%s.log",job.id);
 if((e=path_format(job.status_path,sizeof job.status_path,Y4_BOARD_RUNTIME,status_name))||
 (e=path_format(job.log_path,sizeof job.log_path,Y4_BOARD_STATE,log_name)))return e;
 int log=open(job.log_path,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);if(log<0)return -errno;
 /* Accepted receipt is durable before a helper can perform any hardware IO. */
 job.started_ms=y4_board_now_ms();job.code=-EINPROGRESS;strcpy(job.phase,"accepted");
 e=job_save();if(e){close(log);return e;}
 pid_t parent=getpid(),pid=fork();
 if(pid<0){e=-errno;close(log);return e;}
 if(!pid) {
  if(prctl(PR_SET_PDEATHSIG,SIGTERM)||getppid()!=parent)_exit(126);
  if(dup2(log,STDOUT_FILENO)<0||dup2(log,STDERR_FILENO)<0)_exit(126);
  int input=open("/dev/null",O_RDONLY|O_CLOEXEC);if(input<0||dup2(input,STDIN_FILENO)<0)_exit(126);
  if(input>2)close(input);if(log>2)close(log);
  char *const args[]={Y4_BOARD_MAINTAIN,"--package",job.path,"--key",Y4_BOARD_KEY,
   "--host-version",version,"--status",job.status_path,"--sequence-floor",floor_text,NULL};
  char *const env[]={"PATH=/usr/sbin:/usr/bin:/sbin:/bin","LANG=C","LC_ALL=C",NULL};
  execve(Y4_BOARD_MAINTAIN,args,env);_exit(127);
 }
 close(log);job.pid=pid;strcpy(job.phase,"running");job_save();return 0;
}
static int job_json(char *out,size_t cap) {
 char helper_phase[40]="unavailable",helper[8192];long long helper_status=-ENODATA;
 uint64_t written=0,verified=0,image=0;int counters_known=0;
 if(*job.status_path&&!y4_board_read_file(job.status_path,helper,sizeof helper,NULL)) {
  if(y4_board_json_string(helper,"phase",helper_phase,sizeof helper_phase)||!valid_word(helper_phase))strcpy(helper_phase,"malformed");
  if(y4_board_json_number(helper,"status",&helper_status)||helper_status>0||helper_status< -4095)helper_status=-EPROTO;
  counters_known=!y4_board_json_unsigned(helper,"bytes_written",&written)&&
   !y4_board_json_unsigned(helper,"bytes_verified",&verified)&&
   !y4_board_json_unsigned(helper,"image_bytes",&image)&&written<=image&&verified<=image;
 }
 int progress_valid=counters_known&&image&&(!strcmp(helper_phase,"writing")||!strcmp(helper_phase,"verifying")||!strcmp(helper_phase,"complete"));
 char written_text[32]="null",verified_text[32]="null",image_text[32]="null";
 if(counters_known){(void)snprintf(written_text,sizeof written_text,"%" PRIu64,written);
  (void)snprintf(verified_text,sizeof verified_text,"%" PRIu64,verified);(void)snprintf(image_text,sizeof image_text,"%" PRIu64,image);}
 int n=snprintf(out,cap,"{\"schema\":1,\"job\":\"%s\",\"phase\":\"%s\",\"status\":%d,\"verified\":%s,"
 "\"running\":%s,\"overdue\":%s,\"cancel_requested\":%s,\"blocked\":%s,\"helper_phase\":\"%s\","
 "\"helper_status\":%lld,\"progress_valid\":%s,\"bytes_written\":%s,\"bytes_verified\":%s,\"image_bytes\":%s,"
 "\"version\":\"%s\",\"uid\":\"%s\",\"sequence_valid\":%s,\"sequence\":%" PRIu64 "}\n",
 job.id,*job.phase?job.phase:"idle",job.code,job.verified?"true":"false",job.pid>0?"true":"false",
 job.overdue?"true":"false",job.cancel_requested?"true":"false",job.blocked?"true":"false",helper_phase,helper_status,
 progress_valid?"true":"false",written_text,verified_text,image_text,job.version,job.uid,job.verified?"true":"false",job.sequence);
 return n<0||(size_t)n>=cap?-EOVERFLOW:n;
}
static void discard_upload(struct client *c) {
 if(c->ota){y4_board_ota_abort();c->ota=0;}
 if(c->upload_fd>=0){close(c->upload_fd);c->upload_fd=-1;}
 if(c->upload_reserved){upload_busy=0;c->upload_reserved=0;}
 if(c->digest){EVP_MD_CTX_free(c->digest);c->digest=NULL;}
 /* A submitted MCU job owns the file after upload_finish clears c->path. */
 if(*c->path){(void)unlink(c->path);c->path[0]=0;}
 c->upload_idle_deadline=0;
}
static void client_close(struct client*c) {
 if(c->fd>=0)close(c->fd);
 discard_upload(c);
 explicit_bzero(c,sizeof *c);c->fd=c->upload_fd=-1;
}
static const char *reason(unsigned code) {
 switch(code){case 200:return "OK";case 202:return "Accepted";case 400:return "Bad Request";
 case 403:return "Forbidden";case 404:return "Not Found";case 405:return "Method Not Allowed";
 case 408:return "Request Timeout";case 409:return "Conflict";case 411:return "Length Required";
 case 413:return "Payload Too Large";case 417:return "Expectation Failed";case 500:return "Internal Server Error";
 case 503:return "Service Unavailable";case 507:return "Insufficient Storage";default:return "Error";}
}
static void response(struct client*c,unsigned code,const char *body) {
 int n=snprintf(c->output,sizeof c->output,
 "HTTP/1.1 %u %s\r\nContent-Type: application/json\r\nContent-Length: %zu\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n%s",
 code,reason(code),strlen(body),body);
 if(n<0||(size_t)n>=sizeof c->output){c->response=1;c->out_bytes=0;return;}
 c->response=1;c->out_bytes=(size_t)n;c->out_sent=0;c->deadline=y4_board_now_ms()+15000u;
}
static void error_response(struct client*c,unsigned code,int status,const char *message) {
 char text[256];(void)snprintf(text,sizeof text,"{\"status\":%d,\"error\":\"%s\"}\n",status,message);response(c,code,text);
}
static void upload_error(struct client *c,unsigned code,int e,const char *message) {
 discard_upload(c);
 error_response(c,code,e,message);
}
static void publish_event(enum event_topic topic,const char *data,int result) {
 struct event_value *value=&event_values[topic];
 if(result<=0||(size_t)result>=sizeof value->data)return;
 size_t bytes=(size_t)result;
 while(bytes&&(data[bytes-1u]=='\n'||data[bytes-1u]=='\r'))--bytes;
 if(bytes==value->bytes&&!memcmp(data,value->data,bytes))return;
 memcpy(value->data,data,bytes);
 value->data[bytes]=0;
 value->bytes=bytes;
 ++value->generation;
}
static void resource_tick(uint64_t now) {
 if(now<resource_next)return;
 resource_next=now+RESOURCE_INTERVAL_MS;
 char json[Y4_RESOURCE_JSON_CAP];
 int n=y4_resource_refresh(&resource_sampler,now,json,sizeof json);
 publish_event(EVENT_RESOURCES,json,n);
}
static void refresh_events(uint64_t now) {
 int periodic=now>=event_next;
 if(periodic)event_next=now+RESOURCE_INTERVAL_MS;
 char json[EVENT_DATA_CAP];
 int fresh=sample.magic==Y4_BOARD_SAMPLE_MAGIC&&sample.sampled_ms&&
           now>=sample.sampled_ms&&now-sample.sampled_ms<5000u;
 if(periodic&&(!event_values[EVENT_TEMPERATURE].generation||
               telemetry_sampled_ms!=sample.sampled_ms||telemetry_fresh!=fresh)) {
  int n=y4_board_temperature_json(&sample,json,sizeof json);
  publish_event(EVENT_TEMPERATURE,json,n);
  if(!strcmp(Y4_DEVICE_ID,"Y12")){
   n=y12_board_power_json(&sample,json,sizeof json);
   publish_event(EVENT_SLOT_POWER,json,n);
  }
  fan_event_dirty=1;
  telemetry_sampled_ms=sample.sampled_ms;
  telemetry_fresh=fresh;
 }
 if(fan_event_dirty) {
  int n=y4_board_fans_json(&sample,json,sizeof json);
  publish_event(EVENT_FANS,json,n);
  fan_event_dirty=0;
 }
 int ota_phase=y4_board_ota_phase();
 if(periodic||ota_event_phase!=ota_phase) {
  int n=y4_board_ota_status_json(json,sizeof json);
  publish_event(EVENT_OTA,json,n);
  if(ota_event_phase!=ota_phase)ota_phase_generation=event_values[EVENT_OTA].generation;
  ota_event_phase=ota_phase;
 }
 if(periodic||strcmp(mcu_event_phase,job.phase)||mcu_event_started_ms!=job.started_ms) {
  int n=job_json(json,sizeof json);
  publish_event(EVENT_MCU,json,n);
  memcpy(mcu_event_phase,job.phase,sizeof mcu_event_phase);
  mcu_event_started_ms=job.started_ms;
 }
}

static void event_close(struct event_client *stream) {
 if(stream->fd>=0)close(stream->fd);
 *stream=(struct event_client){.fd=-1};
}

static int event_queue(struct event_client *stream,enum event_topic topic,int opening,uint64_t now) {
 static const char preamble[]=
  "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\n"
  "Connection: close\r\nCache-Control: no-store, no-transform\r\n"
  "X-Accel-Buffering: no\r\n\r\nretry: 2000\n\n";
 const struct event_value *value=&event_values[topic];
 int n=snprintf(stream->output,sizeof stream->output,"%sevent: %s\ndata: %s\n\n",
                opening?preamble:"",event_names[topic],value->data);
 if(n<0||(size_t)n>=sizeof stream->output)return -EOVERFLOW;
 stream->bytes=(size_t)n;
 stream->sent=0;
 stream->deadline=now+EVENT_WRITE_TIMEOUT_MS;
 stream->seen[topic]=value->generation;
 stream->next_topic=((unsigned)topic+1u)%EVENT_TOPIC_COUNT;
 return 0;
}
static int event_queue_next(struct event_client *stream,uint64_t now) {
 if(stream->sent!=stream->bytes)return 0;
 /* Give phase changes a send opportunity before the next OTA apply tick.
  * Progress counters and other observations share the remaining turns. */
 if(stream->seen[EVENT_OTA]<ota_phase_generation)
  return event_queue(stream,EVENT_OTA,0,now);
 for(unsigned offset=0;offset<EVENT_TOPIC_COUNT;offset++) {
  unsigned topic=(stream->next_topic+offset)%EVENT_TOPIC_COUNT;
  if(stream->seen[topic]!=event_values[topic].generation)
   return event_queue(stream,(enum event_topic)topic,0,now);
 }
 return 0;
}

/* Only a validated and root-authenticated HTTP request is promoted.
 * A subscriber holds a streaming slot instead of a regular request slot. */
static void event_subscribe(struct client *c,struct event_client streams[EVENT_CLIENTS]) {
 if(!event_values[EVENT_RESOURCES].generation){error_response(c,503,-EAGAIN,"observation_unavailable");return;}
 for(unsigned i=0;i<EVENT_CLIENTS;i++) {
  struct event_client *stream=&streams[i];
  if(stream->fd>=0)continue;
  /* Keep the in-kernel socket backlog small for lagging readers as well. */
  const int send_buffer=4096;
  if(setsockopt(c->fd,SOL_SOCKET,SO_SNDBUF,&send_buffer,sizeof send_buffer)) {
   error_response(c,503,-errno,"event_stream_unavailable");
   return;
  }
  uint64_t now=y4_board_now_ms();
  refresh_events(now);
  if(event_queue(stream,EVENT_RESOURCES,1,now)) {
   error_response(c,500,-EOVERFLOW,"response_overflow");
   return;
  }
  stream->fd=c->fd;
  c->fd=-1;
  client_close(c);
  return;
 }
 error_response(c,503,-EBUSY,"event_stream_limit");
}

static void event_io(struct event_client *stream,short events,uint64_t now) {
 if(stream->fd<0)return;
 if(events&(POLLERR|POLLNVAL|POLLHUP)) {event_close(stream);return;}
 if(stream->sent==stream->bytes)return;
 if(now>=stream->deadline){event_close(stream);return;}
 if(!(events&POLLOUT))return;
 ssize_t n=send(stream->fd,stream->output+stream->sent,stream->bytes-stream->sent,MSG_NOSIGNAL);
 if(n>0)stream->sent+=(size_t)n;
 else if(n<0&&errno!=EAGAIN&&errno!=EWOULDBLOCK&&errno!=EINTR)event_close(stream);
}
static int initialization_admission(void) {
 char text[8192],current[64],recorded[40];size_t bytes=0;
 int e=y4_board_read_file(Y4_BOARD_INIT_STATUS,text,sizeof text,NULL);if(e)return e;
 int completed=0;if(y4_board_json_bool(text,"completed",&completed)||!completed)return -EAGAIN;
 int retry_blocked=0;
 e=y4_board_json_bool(text,"mcu_retry_blocked",&retry_blocked);
 if(e&&e!=-ENODATA)return -EPROTO;
 if(retry_blocked)return -EOWNERDEAD;
 if(y4_board_json_string(text,"boot_id",recorded,sizeof recorded))return -EPROTO;
 e=y4_board_read_file("/proc/sys/kernel/random/boot_id",current,sizeof current,&bytes);if(e)return e;
 while(bytes&&(current[bytes-1u]=='\n'||current[bytes-1u]=='\r'))current[--bytes]=0;
 if(bytes!=36u||strcmp(recorded,current))return -ESTALE;
 int lock=open(Y4_BOARD_RUNTIME "/boot.lock",O_RDWR|O_NOFOLLOW|O_CLOEXEC);if(lock<0)return -errno;
 struct stat st;e=fstat(lock,&st)?-errno:0;
 if(!e&&(!S_ISREG(st.st_mode)||st.st_uid||st.st_nlink!=1u||(st.st_mode&07777u)!=0600u))e=-EPERM;
 if(!e&&flock(lock,LOCK_EX|LOCK_NB))e=errno==EAGAIN||errno==EWOULDBLOCK?-EAGAIN:-errno;
 close(lock);return e;
}
static int upload_admit(struct client*c) {
 int ready=initialization_admission();if(ready)return ready;
 if(job.pid>0||job.blocked||upload_busy)return -EBUSY;
 DIR*d=opendir(Y4_BOARD_STATE);if(!d)return -errno;unsigned count=0;struct dirent*entry;
 while((entry=readdir(d)))if(!strncmp(entry->d_name,"firmware-",9)&&strstr(entry->d_name,".bin"))count++;
 closedir(d);if(count>=32u)return -ENOSPC;
 struct statvfs space;if(statvfs(Y4_BOARD_STATE,&space))return -errno;
 if((uint64_t)space.f_bavail*(uint64_t)space.f_frsize<c->expected+UINT64_C(4194304))return -ENOSPC;
 unsigned char random[16];ssize_t n=getrandom(random,sizeof random,GRND_NONBLOCK);if(n!=(ssize_t)sizeof random)return n<0?-errno:-EIO;
 for(unsigned i=0;i<16u;i++)(void)snprintf(c->job+i*2u,3,"%02x",random[i]);
 (void)snprintf(c->name,sizeof c->name,"firmware-%s.bin",c->job);
 int e=path_format(c->path,sizeof c->path,Y4_BOARD_STATE,c->name);if(e)return e;
 c->upload_fd=open(c->path,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
 if(c->upload_fd<0){e=-errno;c->path[0]=0;return e;}
 c->upload_reserved=1;upload_busy=1;
 c->digest=EVP_MD_CTX_new();if(!c->digest)return -ENOMEM;
 return EVP_DigestInit_ex(c->digest,EVP_sha256(),NULL)==1?0:-EIO;
}
static void upload_finish(struct client*c) {
 if(fsync(c->upload_fd)){upload_error(c,500,-errno,"upload_fsync_failed");return;}
 if(close(c->upload_fd)){c->upload_fd=-1;upload_error(c,500,-errno,"upload_close_failed");return;}c->upload_fd=-1;
 unsigned char digest[32];unsigned length=0;
 if(EVP_DigestFinal_ex(c->digest,digest,&length)!=1||length!=32u){upload_error(c,500,-EIO,"upload_hash_failed");return;}
 EVP_MD_CTX_free(c->digest);c->digest=NULL;
 c->upload_reserved=0;upload_busy=0;
 c->upload_idle_deadline=0;
 memset(&job,0,sizeof job);memcpy(job.id,c->job,sizeof job.id);memcpy(job.name,c->name,sizeof job.name);memcpy(job.path,c->path,sizeof job.path);
 c->path[0]=0; /* Durable job owns the immutable candidate beyond HTTP close. */
 for(unsigned i=0;i<32u;i++)(void)snprintf(job.sha256+i*2u,3,"%02x",digest[i]);
 int e=launch_helper();
 if(e){job.code=e;job.completed_ms=y4_board_now_ms();strcpy(job.phase,"failed");job_save();error_response(c,503,e,"update_helper_unavailable");return;}
 char body[2048];int n=job_json(body,sizeof body);
 if(n<0)error_response(c,500,n,"response_overflow");else response(c,202,body);
}
static void ota_upload_finish(struct client*c) {
 int e=y4_board_ota_finish();
 if(e){upload_error(c,e==-ENOSPC?507u:500u,e,"ota_launch_failed");return;}
 /* Once accepted, OTA continues independently of the HTTP connection. */
 c->ota=0;
 c->upload_idle_deadline=0;
 char body[1024];int n=y4_board_ota_status_json(body,sizeof body);
 if(n<0)error_response(c,500,n,"response_overflow");else response(c,202,body);
}
/* POST /api/v1/fans body collector: {"policy":"auto"} or
 * {"policy":"manual","fan3":P3,"fan4":P4,"fan5":P5}, duties PERCENT 0..100
 * ("legacy" is accepted to hand the channels back to the built-in cooling
 * policy). The MCU stores them permanently; every restart re-applies the
 * stored policy. The mode write is the commit: duties are written first. */
static void fans_finish(struct client*c) {
 c->fans_post=0;
 c->fans_body[c->received]=0;
 if(!strcmp(Y4_DEVICE_ID,"Y12")) {
  char group[24];
  long long percent;
  if(y4_board_json_string(c->fans_body,"group",group,sizeof group)||
     y4_board_json_number(c->fans_body,"percent",&percent)||percent<0||percent>100||
     (strcmp(group,"pwm-fan-out0")&&strcmp(group,"pwm-fan-out2"))) {
   error_response(c,400,-EBADMSG,"fans_group_or_duty_invalid");return;
  }
  int err=y12_board_set_fan(!strcmp(group,"pwm-fan-out2"),(unsigned)percent,&sample);
  if(err){error_response(c,503,err,"fans_apply_failed");return;}
  fan_event_dirty=1;
  char body[2048];
  if(y4_board_fans_json(&sample,body,sizeof body)<0)
   error_response(c,500,-EOVERFLOW,"response_overflow");
  else response(c,200,body);
  return;
 }
 char policy[16];
 if(y4_board_json_string(c->fans_body,"policy",policy,sizeof policy)||
    (strcmp(policy,"auto")&&strcmp(policy,"manual")&&strcmp(policy,"legacy"))){
  error_response(c,400,-EBADMSG,"fans_policy_invalid");return;}
 unsigned mode=!strcmp(policy,"auto")?1u:!strcmp(policy,"manual")?2u:0u;
 static const char *const keys[3]={"fan3","fan4","fan5"};
 unsigned duty[3]={0,0,0};
 if(mode==2u) {
  for(unsigned i=0;i<3u;i++) {
   long long value;
   if(y4_board_json_number(c->fans_body,keys[i],&value)||value<0||value>100){
    error_response(c,400,-EBADMSG,"fans_duty_invalid");return;}
   duty[i]=(unsigned)value;
  }
 } else {
  for(unsigned i=0;i<3u;i++){long long value;
   if(!y4_board_json_number(c->fans_body,keys[i],&value)){
    error_response(c,400,-EBADMSG,"fans_duty_invalid");return;}}
 }
 struct y4_fan_policy_state state={0};
 int e=y4_fan_policy_apply(mode,duty,&state);
 if(e){
  error_response(c,503,e,e==-EOPNOTSUPP?"fan_policy_unsupported":
   e==-EBUSY?"fans_busy":"fans_apply_failed");return;}
 static const char *const names[3]={"legacy","auto","manual"};
 char body[512];
 int n=snprintf(body,sizeof body,
 "{\"schema\":1,\"device\":\"%s\",\"policy\":{\"mode\":\"%s\",\"status\":%u,"
 "\"stored\":[%u,%u,%u],\"error\":0}}\n",
 Y4_DEVICE_ID,names[state.mode%3u],(unsigned)state.status,
 (unsigned)state.duty[0],(unsigned)state.duty[1],(unsigned)state.duty[2]);
 if(n<0||(size_t)n>=sizeof body)error_response(c,500,-EOVERFLOW,"response_overflow");
 else response(c,200,body);
}
static void ssh_submit(struct client *c, enum y4_ssh_operation operation,
                       const char *body, size_t length) {
 uint64_t id=++ssh_sequence;
 if(!id)id=++ssh_sequence;
 int e=y4_ssh_api_submit(operation,id,body,length);
 if(e){
  unsigned code=e==-EBADMSG?400u:e==-EBUSY?409u:503u;
  const char *detail=e==-EBADMSG?"ssh_request_invalid":e==-EBUSY?"ssh_busy":"ssh_worker_unavailable";
  error_response(c,code,e,detail);
  return;
 }
 c->ssh_request=id;
 c->deadline=y4_board_now_ms()+SSH_REPLY_TIMEOUT_MS;
}
static void ssh_reply(struct client *c, const struct y4_ssh_reply *reply) {
 c->ssh_request=0;
 int e=reply->result;
 if(e) {
  unsigned code=503u;
  const char *detail="ssh_service_unavailable";
  if(reply->operation==Y4_SSH_PASSWORD) {
   detail="ssh_password_change_failed";
   if(e==-EINVAL){code=400u;detail="ssh_password_invalid";}
   else if(e==-ENOENT){code=404u;detail="ssh_user_not_found";}
   else if(e==-EACCES){code=403u;detail="ssh_password_rejected";}
   else if(e==-EBUSY){code=409u;detail="ssh_password_busy";}
  } else if(e==-EPERM || e==-EACCES) {
   code=403u;detail="ssh_unit_access_denied";
  } else if(e==-EBUSY) {
   code=409u;detail="ssh_unit_busy";
  } else if(e==-ETIMEDOUT) {
   code=408u;detail="ssh_unit_timeout";
  } else if(e==-EUCLEAN) {
   detail="ssh_unit_state_uncertain";
  }
  error_response(c,code,e,detail);
  return;
 }
 if(reply->operation==Y4_SSH_PASSWORD) {
  response(c,200,"{\"schema\":1,\"status\":0}\n");
  return;
 }
 const struct y4_ssh_status *status=&reply->status;
 char body[512];
 int n=snprintf(body,sizeof body,
  "{\"schema\":1,\"device\":\"%s\",\"enabled\":%s,\"running\":%s,"
  "\"service_enabled\":%s,\"service_running\":%s,\"socket_enabled\":%s,\"socket_running\":%s}\n",
  Y4_DEVICE_ID,status->enabled?"true":"false",status->running?"true":"false",
  status->service_enabled?"true":"false",status->service_running?"true":"false",
  status->socket_enabled?"true":"false",status->socket_running?"true":"false");
 if(n<0||(size_t)n>=sizeof body)error_response(c,500,-EOVERFLOW,"response_overflow");
 else response(c,200,body);
}
static void upload_bytes(struct client*c,const char*data,size_t bytes) {
 if(c->ssh_operation) {
  if(bytes>c->expected-c->received){error_response(c,400,-EPROTO,"extra_body_bytes");return;}
  memcpy(c->ssh_body+c->received,data,bytes);
  c->received+=bytes;
  if(c->received==c->expected) {
   c->ssh_body[c->received]=0;
   ssh_submit(c,c->ssh_operation,c->ssh_body,c->received);
   c->ssh_operation=Y4_SSH_NONE;
   explicit_bzero(c->ssh_body,sizeof c->ssh_body);
  }
  return;
 }
 if(c->fans_post) {
  if(bytes>c->expected-c->received){error_response(c,400,-EPROTO,"extra_body_bytes");return;}
  if(c->received+bytes>sizeof c->fans_body-1u){error_response(c,413,-E2BIG,"fans_body_too_large");return;}
  memcpy(c->fans_body+c->received,data,bytes);
  c->received+=bytes;
  if(c->received==c->expected)fans_finish(c);
  return;
 }
 if(bytes>c->expected-c->received){upload_error(c,400,-EPROTO,"extra_body_bytes");return;}
 if(c->ota) {
  int e=y4_board_ota_write(data,bytes);
  if(e){upload_error(c,e==-ENOSPC?507u:500u,e,"ota_write_failed");return;}
  c->received+=bytes;
  if(c->received==c->expected)ota_upload_finish(c);
  else c->upload_idle_deadline=y4_board_now_ms()+UPLOAD_IDLE_MS;
  return;
 }
 size_t at=0;while(at<bytes){ssize_t n=write(c->upload_fd,data+at,bytes-at);
  if(n<0){if(errno==EINTR)continue;upload_error(c,500,-errno,"upload_write_failed");return;}
  if(!n){upload_error(c,500,-EIO,"upload_write_failed");return;}at+=(size_t)n;}
 if(EVP_DigestUpdate(c->digest,data,bytes)!=1){upload_error(c,500,-EIO,"upload_hash_failed");return;}
 c->received+=bytes;
 if(c->received==c->expected)upload_finish(c);
 else c->upload_idle_deadline=y4_board_now_ms()+UPLOAD_IDLE_MS;
}
static int number(const char*s,size_t*out) {
 if(!*s||(*s=='0'&&s[1]))return -EINVAL;size_t value=0;
 for(;*s;s++){if(*s<'0'||*s>'9'||value>(SIZE_MAX-(size_t)(*s-'0'))/10u)return -EINVAL;value=value*10u+(size_t)(*s-'0');}
 *out=value;return 0;
}
static void route(struct client*c,char*header) {
 char *line_end=strstr(header,"\r\n");if(!line_end){error_response(c,400,-EPROTO,"bad_request_line");return;}*line_end=0;
 char method[8],path[128],version[16],extra;
 if(sscanf(header,"%7s %127s %15s %c",method,path,version,&extra)!=3||
 (strcmp(version,"HTTP/1.1")&&strcmp(version,"HTTP/1.0"))||strchr(path,'?')||strchr(path,'#')){error_response(c,400,-EPROTO,"bad_request_line");return;}
 int have_length=0;size_t length=0;char *p=line_end+2;
 while(*p) {
  char*end=strstr(p,"\r\n");if(!end){error_response(c,400,-EPROTO,"bad_header");return;}*end=0;if(!*p)break;
  if(*p==' '||*p=='\t'){error_response(c,400,-EPROTO,"folded_header");return;}
  char*colon=strchr(p,':');if(!colon){error_response(c,400,-EPROTO,"bad_header");return;}*colon++=0;
  while(*colon==' '||*colon=='\t')colon++;
  char*tail=colon+strlen(colon);while(tail>colon&&(tail[-1]==' '||tail[-1]=='\t'))*--tail=0;
  if(!strcasecmp(p,"Transfer-Encoding")){error_response(c,400,-EPROTO,"transfer_encoding_unavailable");return;}
  if(!strcasecmp(p,"Expect")){error_response(c,417,-EOPNOTSUPP,"disable_expect_header");return;}
  if(!strcasecmp(p,"Content-Length")){if(have_length++||number(colon,&length)){error_response(c,400,-EPROTO,"bad_content_length");return;}}
  p=end+2;
 }
 if(!strcmp(method,"GET")) {
  if(length){error_response(c,400,-EPROTO,"get_body_unavailable");return;}
  if(!strcmp(path,"/api/v1/ssh")){ssh_submit(c,Y4_SSH_STATUS,NULL,0);return;}
  if(!strcmp(path,"/api/v1/system/resources")){
   if(event_values[EVENT_RESOURCES].generation)response(c,200,event_values[EVENT_RESOURCES].data);
   else error_response(c,503,-EAGAIN,"observation_unavailable");
   return;
  }
  if(!strcmp(path,"/api/v1/events")){c->subscribe=1;return;}
  char body[16000];int n;
  if(!strcmp(path,"/api/v1/hardware"))n=y4_board_sample_json(&sample,body,sizeof body);
  else if(!strcmp(path,"/api/v1/version"))n=y4_board_version_json(&sample,body,sizeof body);
  else if(!strcmp(path,"/api/v1/fans"))n=y4_board_fans_json(&sample,body,sizeof body);
  else if(!strcmp(Y4_DEVICE_ID,"Y12")&&!strcmp(path,"/api/v1/slot-power"))n=y12_board_power_json(&sample,body,sizeof body);
  else if(!strcmp(path,"/api/v1/temperature"))n=y4_board_temperature_json(&sample,body,sizeof body);
  else if(!strcmp(path,"/api/v1/update/status"))n=y4_board_ota_status_json(body,sizeof body);
  else if(!strcmp(path,"/api/v1/mcu/update"))n=job_json(body,sizeof body);
  else if(!strcmp(path,"/api/v1/mcu/firmware")) {
   size_t bytes;int e=y4_board_read_file(Y4_BOARD_STATE "/active.json",body,sizeof body,&bytes);
   if(e==-ENOENT){strcpy(body,"{\"schema\":1,\"active\":false}\n");n=(int)strlen(body);}else n=e?e:(int)bytes;
  } else {error_response(c,404,-ENOENT,"unknown_route");return;}
  if(n<0)error_response(c,503,n,"observation_unavailable");else response(c,200,body);return;
 }
 if(strcmp(method,"POST")){error_response(c,405,-EOPNOTSUPP,"method_unavailable");return;}
 if(!strcmp(path,"/api/v1/mcu/cancel")) {
  if(length){error_response(c,400,-EINVAL,"cancel_body_unavailable");return;}
  if(job.pid<=0){error_response(c,409,-ENOENT,"no_owned_update");return;}
  if(!job.cancel_requested) {
   if(kill(job.pid,SIGTERM)&&errno!=ESRCH){error_response(c,503,-errno,"cancel_signal_failed");return;}
   job.cancel_requested=1;strcpy(job.phase,"cancelling");job_save();
  }
  char body[2048];int n=job_json(body,sizeof body);if(n<0)error_response(c,500,n,"response_overflow");else response(c,202,body);return;
 }
 if(!strcmp(path,"/api/v1/update/confirm")) {
  if(length){error_response(c,400,-EINVAL,"confirm_body_unavailable");return;}
  int e=y4_board_ota_confirm();
  if(e){error_response(c,503,e,"confirm_failed");return;}
  char body[1024];int n=y4_board_ota_status_json(body,sizeof body);
  if(n<0)error_response(c,500,n,"response_overflow");else response(c,200,body);return;
 }
 if(!strcmp(path,"/api/v1/update")) {
  if(!have_length){error_response(c,411,-EINVAL,"content_length_required");return;}
  if(!length){error_response(c,413,-EFBIG,"package_size_rejected");return;}
  int ready=initialization_admission();
  if(ready){error_response(c,503,ready,"initialization_incomplete");return;}
  int e=y4_board_ota_admit(length);
  if(e){error_response(c,e==-EBUSY?409u:e==-ENOSPC?507u:e==-EFBIG?413u:503u,e,"ota_admission_failed");return;}
  uint64_t now=y4_board_now_ms();
  c->expected=length;c->parsed=1;c->ota=1;c->deadline=now+OTA_UPLOAD_TIME_MS;
  c->upload_idle_deadline=now+UPLOAD_IDLE_MS;
  return;
 }
 if(!strcmp(path,"/api/v1/fans")) {
  if(!have_length){error_response(c,411,-EINVAL,"content_length_required");return;}
  if(!length||length>FANS_BODY_LIMIT){error_response(c,413,-E2BIG,"fans_body_too_large");return;}
  if(strcmp(Y4_DEVICE_ID,"Y12")) {
   int ready=initialization_admission();
   if(ready){error_response(c,503,ready,"initialization_incomplete");return;}
  }
  c->expected=length;c->parsed=1;c->fans_post=1;
  c->deadline=y4_board_now_ms()+15000u;
  return;
 }
 enum y4_ssh_operation operation=Y4_SSH_NONE;
 if(!strcmp(path,"/api/v1/ssh"))operation=Y4_SSH_SET;
 else if(!strcmp(path,"/api/v1/ssh/password"))operation=Y4_SSH_PASSWORD;
 if(operation) {
  if(!have_length){error_response(c,411,-EINVAL,"content_length_required");return;}
  if(!length){error_response(c,400,-EINVAL,"ssh_request_invalid");return;}
  if(length>Y4_SSH_BODY_LIMIT){error_response(c,413,-E2BIG,"ssh_body_too_large");return;}
  c->expected=length;
  c->parsed=1;
  c->ssh_operation=operation;
  c->deadline=y4_board_now_ms()+15000u;
  return;
 }
 if(strcmp(path,"/api/v1/mcu/firmware")){error_response(c,404,-ENOENT,"unknown_route");return;}
 /* The Y12 maintainer currently supports signed verification only.
  * Reject before allocating a staging file or accepting a request body. */
 if(!strcmp(Y4_DEVICE_ID,"Y12")){
  error_response(c,503,-EOPNOTSUPP,"mcu_install_unavailable");return;
 }
 if(!have_length){error_response(c,411,-EINVAL,"content_length_required");return;}
 if(!length||length>Y4_BOARD_PACKAGE_LIMIT){error_response(c,413,-EFBIG,"package_size_rejected");return;}
 c->expected=length;
 int e=upload_admit(c);if(e){upload_error(c,e==-EBUSY?409u:e==-ENOSPC?507u:503u,e,"upload_admission_failed");return;}
 uint64_t now=y4_board_now_ms();
 c->parsed=1;c->deadline=now+UPLOAD_TIME_MS;
 c->upload_idle_deadline=now+UPLOAD_IDLE_MS;
}
static void client_read(struct client*c,struct event_client streams[EVENT_CLIENTS]) {
 if(c->parsed) {
  char buf[8192];ssize_t n=recv(c->fd,buf,sizeof buf,0);
  if(n<0){if(errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR)return;client_close(c);return;}
  if(!n){client_close(c);return;}
  int ssh_body=c->ssh_operation!=Y4_SSH_NONE;
  upload_bytes(c,buf,(size_t)n);
  if(ssh_body)explicit_bzero(buf,(size_t)n);
  return;
 }
 ssize_t n=recv(c->fd,c->header+c->header_bytes,HEADER_BYTES-c->header_bytes,0);
 if(n<0){if(errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR)return;client_close(c);return;}
 if(!n){client_close(c);return;}c->header_bytes+=(size_t)n;c->header[c->header_bytes]=0;
 char*end=strstr(c->header,"\r\n\r\n");
 if(!end){if(c->header_bytes==HEADER_BYTES)error_response(c,413,-E2BIG,"header_too_large");return;}
 size_t header=(size_t)(end-c->header)+4u,extra=c->header_bytes-header;
 if(memchr(c->header,0,header)){error_response(c,400,-EPROTO,"nul_header");return;}
 char copy[HEADER_BYTES+1];memcpy(copy,c->header,header);copy[header]=0;route(c,copy);
 if(c->parsed&&!c->response&&extra)upload_bytes(c,c->header+header,extra);
 else if(extra&&!c->parsed&&!c->response){
  c->ssh_request=0;
  error_response(c,400,-EPROTO,"extra_body_bytes");
 }
 if(c->subscribe&&!c->response)event_subscribe(c,streams);
 explicit_bzero(c->header,sizeof c->header);
}
static int listener(void) {
 int fd=socket(AF_UNIX,SOCK_STREAM|SOCK_NONBLOCK|SOCK_CLOEXEC,0);if(fd<0)return -errno;
 struct sockaddr_un address={.sun_family=AF_UNIX};strcpy(address.sun_path,Y4_BOARD_SOCKET);
 struct stat st;
 if(!lstat(Y4_BOARD_SOCKET,&st)) {
  if(!S_ISSOCK(st.st_mode)||st.st_uid||(st.st_mode&07777u)!=0600u){close(fd);return -EPERM;}
  int probe=socket(AF_UNIX,SOCK_STREAM|SOCK_NONBLOCK|SOCK_CLOEXEC,0);if(probe<0){close(fd);return -errno;}
  int rc=connect(probe,(struct sockaddr*)&address,sizeof address);int saved=errno;close(probe);
  if(!rc||saved!=ECONNREFUSED){close(fd);return -EADDRINUSE;}
  if(unlink(Y4_BOARD_SOCKET)){int e=-errno;close(fd);return e;}
 }else if(errno!=ENOENT){int e=-errno;close(fd);return e;}
 if(bind(fd,(struct sockaddr*)&address,sizeof address)){int e=-errno;close(fd);return e;}
 if(lstat(Y4_BOARD_SOCKET,&st)){int e=-errno;close(fd);return e;}
 socket_owned=1;socket_device=st.st_dev;socket_inode=st.st_ino;
 if(chmod(Y4_BOARD_SOCKET,0600)||listen(fd,8)){int e=-errno;close(fd);return e;}
 return fd;
}
static void accept_clients(int fd,struct client clients[CLIENTS]) {
 for(unsigned a=0;a<CLIENTS;a++) {
  int peer=accept4(fd,NULL,NULL,SOCK_NONBLOCK|SOCK_CLOEXEC);
  if(peer<0)return;
  struct ucred cred;socklen_t bytes=sizeof cred;
  if(getsockopt(peer,SOL_SOCKET,SO_PEERCRED,&cred,&bytes)||bytes!=sizeof cred||cred.uid!=0){close(peer);continue;}
  unsigned at;for(at=0;at<CLIENTS;at++)if(clients[at].fd<0)break;
  if(at==CLIENTS){close(peer);continue;}
  clients[at].fd=peer;clients[at].deadline=y4_board_now_ms()+15000u;
 }
}
static int board_matches_device_tree(void)
{
 char compatible[4096];
 FILE *f=fopen("/proc/device-tree/compatible","rb");
 if(!f)return strcmp(Y4_DEVICE_ID,"Y12")!=0;
 size_t len=fread(compatible,1,sizeof compatible,f);
 int valid=!ferror(f)&&feof(f)&&len&&compatible[len-1]==0;
 fclose(f);
 if(!valid)return 0;
 /* The primary compatible identifies the product. Secondary entries
  * describe compatible platform interfaces, not a second product. */
 if(!strcmp(Y4_DEVICE_ID,"Y12"))return !strcmp(compatible,"myt,y12");
 return !strcmp(compatible,"myt,y4");
}

int main(int argc,char **argv) {
 (void)argv;if(argc!=1||geteuid()){fputs("y4-boardd requires root and no arguments\n",stderr);return 2;}
 if(!board_matches_device_tree()){fputs("boardd target differs from device tree\n",stderr);return 2;}
 umask(077);int e=y4_board_private_dir(Y4_BOARD_RUNTIME);if(!e)e=y4_board_private_dir(Y4_BOARD_STATE);
 if(e){fprintf(stderr,"y4-board directory rejected status=%d\n",e);return 1;}
 int lease=secure_lock("daemon.lock");if(lease<0){fprintf(stderr,"y4-board daemon lease rejected status=%d\n",lease);return 1;}
 struct sigaction sa={.sa_handler=stop};sigemptyset(&sa.sa_mask);
 if(sigaction(SIGTERM,&sa,NULL)||sigaction(SIGINT,&sa,NULL)){close(lease);return 1;}
 if(signal(SIGPIPE,SIG_IGN)==SIG_ERR){close(lease);return 1;}
 e=job_restore();if(e){job.blocked=1;job.code=e;strcpy(job.phase,"receipt_invalid");}
 (void)y4_board_slot_boot_count();
 /* PCIe sampler is read-only and isolated from MCU and HTTP admission. */
 pid_t pcie=y4_board_pcie_start();if(pcie<0){fprintf(stderr,"y4-board pcie worker unavailable status=%ld\n",(long)pcie);pcie=0;}
 int channels[2];if(socketpair(AF_UNIX,SOCK_SEQPACKET|SOCK_NONBLOCK|SOCK_CLOEXEC,0,channels)){close(lease);return 1;}
 pid_t parent=getpid(),monitor=fork();int monitor_unknown=0;
 if(monitor<0){close(channels[0]);close(channels[1]);close(lease);return 1;}
 if(!monitor) {
  close(channels[0]);close(lease);y4_board_pcie_child_close();
  if(prctl(PR_SET_PDEATHSIG,SIGTERM)||getppid()!=parent)_exit(124);
  int rc=y4_board_monitor_run(channels[1]);_exit(rc?1:0);
 }
 close(channels[1]);int server=listener();
 if(server<0){fprintf(stderr,"y4-board API listener failed status=%d\n",server);y4_board_stopping=1;}
 struct client clients[CLIENTS];memset(clients,0,sizeof clients);
 for(unsigned i=0;i<CLIENTS;i++)clients[i].fd=clients[i].upload_fd=-1;
 struct event_client streams[EVENT_CLIENTS];
 for(unsigned i=0;i<EVENT_CLIENTS;i++)streams[i]=(struct event_client){.fd=-1};
 while(!y4_board_stopping) {
  job_tick();y4_board_ota_tick();(void)y4_board_pcie_tick(&pcie);
  if(sample.magic==Y4_BOARD_SAMPLE_MAGIC)(void)y4_board_slot_confirm_tick(&sample);
  if(monitor>0){int ws;pid_t got=waitpid(monitor,&ws,WNOHANG);
   if(got==monitor){monitor=0;fprintf(stderr,"y4-board monitor exited raw_status=%d; observations will expire\n",ws);}
   else if(got<0&&errno!=EINTR){monitor_unknown=1;fprintf(stderr,"y4-board monitor ownership lost pid=%ld status=%d\n",(long)monitor,-errno);monitor=0;}}
  struct y4_ssh_reply ssh;
  if(y4_ssh_api_take(&ssh)) {
   for(unsigned i=0;i<CLIENTS;i++) {
    if(clients[i].fd>=0 && clients[i].ssh_request==ssh.id) {
     ssh_reply(&clients[i],&ssh);
     break;
    }
   }
  }
  uint64_t now=y4_board_now_ms();
  resource_tick(now);
  struct pollfd fds[2+CLIENTS+EVENT_CLIENTS];fds[0]=(struct pollfd){server,POLLIN,0};fds[1]=(struct pollfd){monitor>0?channels[0]:-1,POLLIN,0};
  for(unsigned i=0;i<CLIENTS;i++)fds[i+2u]=(struct pollfd){clients[i].fd,clients[i].response?POLLOUT:clients[i].ssh_request?0:POLLIN,0};
  for(unsigned i=0;i<EVENT_CLIENTS;i++) {
   if(streams[i].fd>=0){refresh_events(now);break;}
  }
  for(unsigned i=0;i<EVENT_CLIENTS;i++) {
   struct event_client *stream=&streams[i];
   if(stream->fd>=0&&event_queue_next(stream,now))event_close(stream);
   fds[2u+CLIENTS+i]=(struct pollfd){stream->fd,stream->sent<stream->bytes?POLLOUT:0,0};
  }
  int ready=poll(fds,2u+CLIENTS+EVENT_CLIENTS,100);if(ready<0&&errno!=EINTR){fprintf(stderr,"y4-board poll failed status=%d\n",-errno);break;}
  if(fds[0].revents&POLLIN)accept_clients(server,clients);
  if(fds[1].revents&POLLIN)for(unsigned n=0;n<4u;n++) {
   struct y4_board_sample next;ssize_t got=recv(channels[0],&next,sizeof next,MSG_DONTWAIT|MSG_TRUNC);
   if(got<0)break;
   if(got==(ssize_t)sizeof next&&next.magic==Y4_BOARD_SAMPLE_MAGIC){
    if(!strcmp(Y4_DEVICE_ID,"Y12"))y12_board_fans_preserve_readback(&sample,&next);
    sample=next;
   }
  }
  now=y4_board_now_ms();
  for(unsigned i=0;i<EVENT_CLIENTS;i++)event_io(&streams[i],fds[2u+CLIENTS+i].revents,now);
  for(unsigned i=0;i<CLIENTS;i++) {
   struct client*c=&clients[i];if(c->fd<0)continue;
   if(fds[i+2u].revents&(POLLERR|POLLNVAL)){client_close(c);continue;}
   if((fds[i+2u].revents&POLLIN)&&!c->response) {
    client_read(c,streams);
   }
   if(c->fd<0)continue;
   if(c->response&&(fds[i+2u].revents&POLLOUT)) {
    ssize_t sent=send(c->fd,c->output+c->out_sent,c->out_bytes-c->out_sent,MSG_NOSIGNAL);
    if(sent>0)c->out_sent+=(size_t)sent;
    else if(sent<0&&errno!=EAGAIN&&errno!=EWOULDBLOCK&&errno!=EINTR){client_close(c);continue;}
    if(c->out_sent==c->out_bytes){client_close(c);continue;}
   }
   int upload_idle=c->upload_idle_deadline&&now>=c->upload_idle_deadline;
   if(now>=c->deadline||upload_idle){
    if(c->response)client_close(c);
    else {
     c->ssh_request=0;
     discard_upload(c);
     error_response(c,408,-ETIMEDOUT,upload_idle?"upload_idle_timeout":"request_deadline");
    }
   }
   else if(fds[i+2u].revents&POLLHUP)client_close(c);
  }
 }
 if(server>=0)close(server);
 if(socket_owned){struct stat current;if(!lstat(Y4_BOARD_SOCKET,&current)&&current.st_dev==socket_device&&current.st_ino==socket_inode)(void)unlink(Y4_BOARD_SOCKET);}
 for(unsigned i=0;i<CLIENTS;i++)client_close(&clients[i]);
 for(unsigned i=0;i<EVENT_CLIENTS;i++)event_close(&streams[i]);
 if(job.pid>0&&!job.cancel_requested){(void)kill(job.pid,SIGTERM);job.cancel_requested=1;strcpy(job.phase,"cancelling");job_save();}
 /* Do not abandon an active flash helper or claim its exit on a timeout. */
 while(job.pid>0){job_tick();struct timespec t={0,100000000};(void)nanosleep(&t,NULL);}
 if(monitor>0&&!monitor_unknown) {
  (void)kill(monitor,SIGTERM);
  for(unsigned n=0;n<20u;n++){int ws;pid_t got=waitpid(monitor,&ws,WNOHANG);if(got==monitor){monitor=0;break;}
   struct timespec t={0,100000000};(void)nanosleep(&t,NULL);}
  if(monitor>0)fprintf(stderr,"y4-board monitor exit pending pid=%ld; bus lock remains kernel-owned\n",(long)monitor);
 }
 int pcie_exit=y4_board_pcie_stop(&pcie);if(pcie_exit)fprintf(stderr,"y4-board pcie exit pending status=%d pid=%ld\n",pcie_exit,(long)pcie);
 close(channels[0]);close(lease);return server<0?1:0;
}
