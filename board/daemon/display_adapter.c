/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
#include "board_daemon.h"
#include "../display/y4_display.h"
#include "vendor/y4_os_mcu.h"
#include "vendor/y4_os_mcu_ix_display.h"
#include "vendor/y4_os_ix_thermal.h"
#include <arpa/inet.h>
#include <errno.h>
#include <string.h>
int y4_board_display_publish(int fd,const struct y4_board_sample *s) {
 if(fd<0||!s)return -EINVAL;
 const struct y4_network_snapshot *n=&s->network;uint64_t now=y4_board_now_ms();
 uint64_t age=n->sampled_ms&&now>=n->sampled_ms?now-n->sampled_ms:UINT64_MAX;
 struct y4_display_input v={.max_attempts=5,.sample_age_ms=age>65535u?65535u:(uint16_t)age,
  .network_error=n->error?n->error:age>=3000u?-ESTALE:0};
 if(age<3000u) {
  for(unsigned i=0;i<3u;i++) {
   const struct y4_network_link*l=&n->physical[i];if(l->valid)v.physical_valid_mask|=(uint8_t)(1u<<i);
   /* present stays device existence. No native plug-state source is
    * verified for these ports without hardware access, so nothing here may
    * impersonate insertion; the view renders an unproven plug as unknown. */
   if(l->valid&&l->present)v.physical_present_mask|=(uint8_t)(1u<<i);
   if(l->valid&&l->present&&l->carrier)v.physical_carrier_mask|=(uint8_t)(1u<<i);
  }
  if(!n->route_error)v.default_egress=(uint8_t)n->default_egress;
  if(!n->bond_state_error){v.bond_state=(uint8_t)n->bond_state;
   v.bond_active_mask=(uint8_t)n->bond_active_mask;v.bond_lacp_mask=(uint8_t)n->bond_lacp_mask;}
 }
 struct y4_board_initialization init;int init_error=y4_board_initialization_read(&init);
 if(!init_error&&init.valid) {
  v.initialization_valid=1;v.attempt=(uint8_t)init.attempt;v.initialization_error=init.status;
  if(init.completed)v.initialization_state=init.status?VIEW_INIT_FAILED:VIEW_INIT_READY;
  else if(!strcmp(init.phase,"failed")||strstr(init.phase,"unknown"))v.initialization_state=VIEW_INIT_FAILED;
  else if(strstr(init.phase,"mcu"))v.initialization_state=VIEW_INIT_MCU;
  else if(strstr(init.phase,"nic")||strstr(init.phase,"efuse"))v.initialization_state=VIEW_INIT_NIC;
  else v.initialization_state=VIEW_INIT_CHECKING;
 }
 for(unsigned i=0;i<2u;i++) {
  const struct y4_network_interface*l=&n->logical[i];
  int physical_known,physical_carrier;
  if(i==0u){physical_known=n->physical[0].valid;physical_carrier=physical_known&&n->physical[0].present&&n->physical[0].carrier;}
  else {physical_carrier=(n->physical[1].valid&&n->physical[1].present&&n->physical[1].carrier)||
                         (n->physical[2].valid&&n->physical[2].present&&n->physical[2].carrier);
        physical_known=physical_carrier||(n->physical[1].valid&&n->physical[2].valid);}
  if(age>=3000u||!l->link.valid||!physical_known)v.logical_state[i]=VIEW_NET_UNKNOWN;
  else if(!physical_carrier||!l->link.present||!l->link.carrier)v.logical_state[i]=VIEW_NET_DISCONNECTED;
  else if(v.initialization_valid&&!init.completed)v.logical_state[i]=VIEW_NET_INITIALIZING;
  else if(l->ipv4_error||n->address_error)v.logical_state[i]=VIEW_NET_FAILED;
  else if(!l->ipv4_valid) {
   /* A missing kernel address is not evidence of an ongoing DHCP exchange.
    * The phase comes from the read-only manager observation; without it only
    * the observed fact (carrier up, no address) is reported. */
   const struct y4_network_manager_state *m=&s->nm[i];
   if(m->valid&&m->state==120)v.logical_state[i]=VIEW_NET_FAILED;
   else if(m->valid&&m->state>=40&&m->state<=90)v.logical_state[i]=VIEW_NET_WAITING;
   else if(m->valid&&m->state==100)v.logical_state[i]=VIEW_NET_NO_ADDRESS;
   else if(m->valid&&(m->state==10||m->state==20||m->state==30))v.logical_state[i]=VIEW_NET_DISCONNECTED;
   else v.logical_state[i]=VIEW_NET_NO_ADDRESS;
  }
  else {
   struct in_addr ip;if(l->prefix_length>=1u&&l->prefix_length<=32u&&inet_pton(AF_INET,l->ipv4,&ip)==1) {
    v.logical_state[i]=VIEW_NET_READY;v.ipv4_valid_mask|=(uint8_t)(1u<<i);
    memcpy(v.ipv4[i],&ip,4);v.prefix_length[i]=(uint8_t)l->prefix_length;
   } else v.logical_state[i]=VIEW_NET_FAILED;
  }
 }
 int result=y4_display_publish(fd,&v);
 /* Independent temperature frame never grants temperature-based PWM. */
 struct y4_mcu_identity id={{s->firmware[0],s->firmware[1],s->firmware[2]},128u};
 struct y4_ix_thermal_sample ix={0};(void)y4_ix_thermal_read(&ix);
 int ix_status=y4_mcu_ix_display(fd,&id,&ix);
 return result?result:ix_status;
}
