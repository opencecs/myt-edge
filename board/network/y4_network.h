/* SPDX-License-Identifier: Apache-2.0 */
#ifndef Y4_NETWORK_H
#define Y4_NETWORK_H
#include <stdint.h>
#include <net/if.h>
#include <netinet/in.h>

enum y4_network_physical_role { Y4_NET_LAN=0, Y4_NET_SFP1=1, Y4_NET_SFP2=2, Y4_NET_PHYSICAL_COUNT=3 };
enum y4_network_logical_role { Y4_NET_LOGICAL_LAN=0, Y4_NET_LOGICAL_BOND=1, Y4_NET_LOGICAL_COUNT=2 };
enum y4_network_bond_state { Y4_NET_BOND_UNKNOWN=0, Y4_NET_BOND_DOWN=1, Y4_NET_BOND_NEGOTIATING=2, Y4_NET_BOND_INDIVIDUAL=3, Y4_NET_BOND_LACP_SINGLE=4, Y4_NET_BOND_LACP_MULTI=5 };
enum y4_network_egress { Y4_NET_EGRESS_NONE=0, Y4_NET_EGRESS_LAN=1, Y4_NET_EGRESS_BOND=2, Y4_NET_EGRESS_OTHER=3, Y4_NET_EGRESS_AMBIGUOUS=4 };
#define Y4_NET_BOND_NAME "bond10g"
/* All data is from the current bounded kernel/sysfs sample. A zero IP flag
 * means no usable IPv4 address was observed, never a cached earlier address.
 * error is a negative errno; valid means link identity/carrier was observed.
 * Missing hardware has present=0, valid=1; unavailable observations valid=0. */
struct y4_network_link {
    int valid, present, carrier, error;
    unsigned ifindex, master_ifindex, speed_mbps;
    char name[IF_NAMESIZE];
    uint8_t mac[6];int mac_valid; /* permanent interface address (netlink) */
    /* Kernel statistics counters (/sys/class/net/<name>/statistics), read
     * in the same bounded sample; counters_error is negative errno and the
     * values are then not from this cycle. Monotonic since host boot. */
    uint64_t rx_bytes,tx_bytes,rx_packets,tx_packets;
    int counters_error;
};
struct y4_network_interface {
    struct y4_network_link link;
    int ipv4_valid, ipv4_error;
    char ipv4[INET_ADDRSTRLEN];
    unsigned prefix_length;
    char gateway[INET_ADDRSTRLEN];int gateway_valid; /* default-route gateway */
    unsigned gateway_metric;
};
struct y4_network_snapshot {
    uint64_t sampled_ms; /* CLOCK_BOOTTIME, including suspend, shared with MCU GUI age. */
    int error, address_error, route_error;
    struct y4_network_link physical[Y4_NET_PHYSICAL_COUNT];
    struct y4_network_interface logical[Y4_NET_LOGICAL_COUNT];
    enum y4_network_egress default_egress;
    unsigned default_ifindex, default_metric, default_mask, bond_mode;
    /* Masks use physical-role bits: SFP1=2, SFP2=4. LACP_MULTI requires
     * both actor+partner Collecting/Distributing in the SAME active aggregator.
     * Two physical carriers alone never establish aggregation. */
    enum y4_network_bond_state bond_state;
    int bond_state_error, bond_ad_info_valid, bond_partner_present;
    unsigned bond_aggregator_id, bond_aggregator_ports, bond_partner_key;
    unsigned bond_active_mask, bond_lacp_mask;
    uint8_t bond_partner_mac[6];
};
/* Read-only, no exec and no configuration. <= 750 ms total netlink IO budget.
 * The caller must not treat a negative return or field error as link-down. */
int y4_network_sample(struct y4_network_snapshot *out);
#endif
