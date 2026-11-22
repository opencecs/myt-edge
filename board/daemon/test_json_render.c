/* Host smoke test: render the new/extended JSON builders with a synthetic
 * sample and validate the output is parseable JSON. */
#include "board_daemon.h"
#include <stdio.h>
#include <string.h>
int main(void) {
 struct y4_board_sample s;
 memset(&s,0,sizeof s);
 s.magic=Y4_BOARD_SAMPLE_MAGIC;
 s.sampled_ms=y4_board_now_ms();
 s.completed_ms=s.sampled_ms+3;
 s.mcu_error=0;s.uid_valid=1;
 for(unsigned i=0;i<12;i++)s.uid[i]=(uint8_t)(i*3+1);
 memcpy(s.firmware,(uint8_t[]){0,1,39},3);
 s.fault_bits=0;s.fan_mask=6;s.tach_mask=6;
 s.requested_pwm=50;
 s.effective_pwm[0]=30;s.effective_pwm[1]=50;s.effective_pwm[2]=80;s.effective_pwm[3]=100;
 s.rpm[1]=1450;s.rpm[2]=1390;
 s.fan_policy_mode=2;s.fan_policy_status=2;
 s.fan_policy_duty[0]=30;s.fan_policy_duty[1]=50;s.fan_policy_duty[2]=80;
 s.fan_policy_error=0;
 s.fan1_valid=1;s.fan1_rpm=1234;s.fan1_error=0;
 s.cpu=(struct y4_board_temperature){1,0,61500,0};
 s.inside=(struct y4_board_temperature){1,0,38500,0};
 s.pcie_switch=(struct y4_board_temperature){1,0,52000,120};
 s.network.sampled_ms=s.sampled_ms;
 s.network.error=0;s.network.address_error=0;s.network.route_error=0;
 for(unsigned i=0;i<3;i++){
  struct y4_network_link*l=&s.network.physical[i];
  l->valid=1;l->present=1;l->carrier=1;l->ifindex=(unsigned)(2+i);
  l->speed_mbps=i?10000u:2500u;l->mac_valid=1;
  for(unsigned k=0;k<6;k++)l->mac[k]=(uint8_t)(0x10+k+i);
  l->rx_bytes=1234567890123ull+i;l->tx_bytes=987654321ull+i;
  l->rx_packets=7777777+i;l->tx_packets=6666666+i;l->counters_error=0;
 }
 s.network.logical[1].link=s.network.physical[1];
 s.network.logical[1].link.rx_bytes*=3;
 char buf[24576];
 int n=y4_board_sample_json(&s,buf,sizeof buf);
 if(n<0){printf("sample_json failed %d\n",n);return 1;}
 fputs(buf,stdout);
 n=y4_board_fans_json(&s,buf,sizeof buf);
 if(n<0){printf("fans_json failed %d\n",n);return 1;}
 fputs(buf,stdout);
 n=y4_board_temperature_json(&s,buf,sizeof buf);
 if(n<0){printf("temperature_json failed %d\n",n);return 1;}
 fputs(buf,stdout);
 /* stale sample path */
 s.sampled_ms-=10000;
 n=y4_board_fans_json(&s,buf,sizeof buf);
 if(n<0){printf("stale fans_json failed %d\n",n);return 1;}
 fputs(buf,stdout);
 /* old-MCU path */
 s.fan_policy_error=-95;
 n=y4_board_fans_json(&s,buf,sizeof buf);
 if(n<0){printf("unsupported fans_json failed %d\n",n);return 1;}
 fputs(buf,stdout);
 return 0;
}
