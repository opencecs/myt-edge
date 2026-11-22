/*
 * IX topology reducer (pure, bounded, allocation-free).
 *
 * Reduces an already-captured bounded PCI inventory into the IX-02
 * topology contract. No I/O, no filesystem access, no heap, no unbounded
 * loops. Link fields in the input are never read, never copied and never
 * substituted with expected values; all output indices refer to the
 * caller's input array.
 *
 * Upstream identification: the IX switch upstream port is the node with
 * vendor 0x1ec8, device 0x8024, class 0x060400 whose captured parent is
 * the SoC node (vendor 0x1f6c, device 0x0001, class 0x060400). All other
 * IX bridges must be direct children of that upstream port; at most
 * Y4_IX_DOWNSTREAM_MAX of them are accepted, and any nested or
 * disconnected extra IX bridge prevents READY.
 *
 * Downstream indices are sorted by BDF string for a deterministic query
 * order. This is NOT PCB silkscreen / physical connector order.
 */
#include "y4_os_ix_topology.h"

#include <errno.h>
#include <string.h>

/* Identification constants fixed by the platform contract. */
#define Y4_IX_IX_VENDOR_ID  0x1ec8u
#define Y4_IX_IX_DEVICE_ID  0x8024u
#define Y4_IX_SOC_VENDOR_ID 0x1f6cu
#define Y4_IX_SOC_DEVICE_ID 0x0001u
#define Y4_IX_BRIDGE_CLASS  0x060400u

/* Identity field bounds: vendor/device are 16-bit, class is 24-bit. */
#define Y4_IX_ID16_MAX 0xffffu
#define Y4_IX_ID24_MAX 0xffffffu

/* Reset every field of the output (full stale-reuse defence). */
static void topo_reset(struct y4_ix_topology *o)
{
    o->stage = Y4_IX_TOPOLOGY_UNSET;
    o->error = 0;
    o->upstream_index = -1;
    o->soc_parent_index = -1;
    for (unsigned i = 0u; i < Y4_IX_DOWNSTREAM_MAX; ++i)
        o->downstream_index[i] = -1;
    o->nodes_seen = 0u;
    o->ix_nodes = 0u;
    o->downstream_count = 0u;
}

static void topo_stage(struct y4_ix_topology *o, enum y4_ix_topology_stage st, int err)
{
    o->stage = st;
    o->error = (int32_t)err;
}

static int node_is_ix(const struct y4_ix_node *nd)
{
    return nd->vendor == Y4_IX_IX_VENDOR_ID &&
           nd->device == Y4_IX_IX_DEVICE_ID &&
           nd->pci_class == Y4_IX_BRIDGE_CLASS;
}

static int node_is_soc(const struct y4_ix_node *nd)
{
    return nd->vendor == Y4_IX_SOC_VENDOR_ID &&
           nd->device == Y4_IX_SOC_DEVICE_ID &&
           nd->pci_class == Y4_IX_BRIDGE_CLASS;
}

/* Insertion sort of at most Y4_IX_DOWNSTREAM_MAX indices by BDF string. */
static void sort_indices_by_bdf(const struct y4_ix_node *nodes, int32_t *idx, size_t count)
{
    for (size_t a = 1u; a < count; ++a) {
        int32_t v = idx[a];
        size_t b = a;
        while (b > 0u &&
               strcmp(nodes[(size_t)idx[b - 1u]].bdf, nodes[(size_t)v].bdf) > 0) {
            idx[b] = idx[b - 1u];
            --b;
        }
        idx[b] = v;
    }
}

/*
 * Collect direct IX children of node `parent`. Returns the total count
 * found; at most Y4_IX_DOWNSTREAM_MAX indices are written to out_idx
 * (sorted by BDF). Bounded O(N) scan, no recursion.
 */
static size_t collect_ix_children(const struct y4_ix_node *nodes, size_t n,
                                  const int32_t *parent_idx, int32_t parent,
                                  int32_t *out_idx)
{
    size_t total = 0u;
    size_t kept = 0u;
    for (size_t j = 0u; j < n; ++j) {
        if ((size_t)parent_idx[j] != (size_t)parent || !node_is_ix(&nodes[j]))
            continue;
        if (kept < (size_t)Y4_IX_DOWNSTREAM_MAX)
            out_idx[kept++] = (int32_t)j;
        ++total;
    }
    sort_indices_by_bdf(nodes, out_idx, kept);
    return total;
}

int y4_ix_topology_reduce(const struct y4_ix_node *nodes, size_t n,
                          struct y4_ix_topology *out)
{
    if (out == NULL)
        return -EINVAL;
    topo_reset(out);

    if (nodes == NULL && n > 0u) {
        topo_stage(out, Y4_IX_TOPOLOGY_ERROR, -EINVAL);
        return -EINVAL;
    }
    if (n > (size_t)Y4_IX_TOPOLOGY_MAX_NODES) {
        topo_stage(out, Y4_IX_TOPOLOGY_ERROR, -EINVAL);
        return -EINVAL;
    }

    /* Pass 1: per-node identity and field validation. */
    for (size_t i = 0u; i < n; ++i) {
        const struct y4_ix_node *nd = &nodes[i];
        if (nd->identity_error < 0) {
            /* Uncertain inventory: fail with the captured errno. */
            int err = (int)nd->identity_error;
            topo_stage(out, Y4_IX_TOPOLOGY_ERROR, err);
            return err;
        }
        if (nd->identity_error > 0) {
            topo_stage(out, Y4_IX_TOPOLOGY_ERROR, -EINVAL);
            return -EINVAL;
        }
        if (!y4_ix_bdf_valid(nd->bdf)) {
            topo_stage(out, Y4_IX_TOPOLOGY_ERROR, -EINVAL);
            return -EINVAL;
        }
        /* Empty parent (root) is allowed; otherwise it must be canonical. */
        if (nd->parent_bdf[0] != '\0' && !y4_ix_bdf_valid(nd->parent_bdf)) {
            topo_stage(out, Y4_IX_TOPOLOGY_ERROR, -EINVAL);
            return -EINVAL;
        }
        if (nd->vendor > Y4_IX_ID16_MAX || nd->device > Y4_IX_ID16_MAX ||
            nd->pci_class > Y4_IX_ID24_MAX) {
            topo_stage(out, Y4_IX_TOPOLOGY_ERROR, -EINVAL);
            return -EINVAL;
        }
    }

    /* Pass 2: duplicate BDF detection, bounded O(N^2), no heap. */
    for (size_t i = 0u; i < n; ++i) {
        for (size_t j = i + 1u; j < n; ++j) {
            if (strcmp(nodes[i].bdf, nodes[j].bdf) == 0) {
                topo_stage(out, Y4_IX_TOPOLOGY_ERROR, -EEXIST);
                return -EEXIST;
            }
        }
    }

    /*
     * Pass 3: precompute parent indices, bounded O(N^2). A parent BDF
     * that is not present in the inventory leaves the index at -1
     * (missing parent is handled by the role logic below, not here).
     */
    int32_t parent_idx[Y4_IX_TOPOLOGY_MAX_NODES];
    for (size_t i = 0u; i < n; ++i) {
        parent_idx[i] = -1;
        if (nodes[i].parent_bdf[0] == '\0')
            continue;
        for (size_t j = 0u; j < n; ++j) {
            if (strcmp(nodes[i].parent_bdf, nodes[j].bdf) == 0) {
                parent_idx[i] = (int32_t)j;
                break;
            }
        }
    }

    /*
     * Pass 4: bounded cycle detection over the parent forest. Each walk
     * is stamped with a unique walk id, so no reset pass is needed and
     * the whole pass is bounded O(N^2) worst case with O(N) memory.
     */
    int32_t mark[Y4_IX_TOPOLOGY_MAX_NODES];
    for (size_t i = 0u; i < n; ++i)
        mark[i] = 0;
    int32_t walk = 0;
    for (size_t i = 0u; i < n; ++i) {
        ++walk;
        int32_t j = (int32_t)i;
        size_t steps = 0u;
        while (j >= 0 && mark[j] != walk) {
            mark[j] = walk;
            j = parent_idx[j];
            if (++steps > n)
                break; /* bounded guard; unreachable with walk stamps */
        }
        if (j >= 0 && mark[j] == walk) {
            topo_stage(out, Y4_IX_TOPOLOGY_ERROR, -ELOOP);
            return -ELOOP;
        }
    }

    out->nodes_seen = (uint32_t)n;

    /* Pass 5: classify IX nodes (exact 1ec8:8024, class 060400 only). */
    uint32_t ix_count = 0u;
    for (size_t i = 0u; i < n; ++i) {
        if (node_is_ix(&nodes[i]))
            ++ix_count;
    }
    out->ix_nodes = ix_count;

    if (ix_count == 0u) {
        /* Nothing discovered yet: keep polling the inventory. */
        topo_stage(out, Y4_IX_TOPOLOGY_INITIALIZING, -EAGAIN);
        return -EAGAIN;
    }

    /* Pass 6: upstream candidates (IX bridge with captured SoC parent). */
    int32_t upstream = -1;
    for (size_t i = 0u; i < n; ++i) {
        if (!node_is_ix(&nodes[i]))
            continue;
        if (parent_idx[i] < 0 || !node_is_soc(&nodes[parent_idx[i]]))
            continue;
        if (upstream >= 0) {
            /* Multiple SoC-attached IX bridges: role indices stay clear. */
            topo_stage(out, Y4_IX_TOPOLOGY_AMBIGUOUS, -ENOTUNIQ);
            return -ENOTUNIQ;
        }
        upstream = (int32_t)i;
    }

    if (upstream < 0) {
        /*
         * No IX bridge with a proved SoC parent: PARTIAL with every role
         * index left at -1. Identity alone never fabricates a role; a
         * 1ec8:8024 node whose 1f6c:0001 class-060400 parent is missing
         * from the inventory is NOT promoted to upstream.
         */
        topo_stage(out, Y4_IX_TOPOLOGY_PARTIAL, -EAGAIN);
        return -EAGAIN;
    }

    out->upstream_index = upstream;
    out->soc_parent_index = parent_idx[upstream];

    /* Pass 7: account for every other IX bridge relative to upstream. */
    int32_t kids[Y4_IX_DOWNSTREAM_MAX];
    size_t child_count = collect_ix_children(nodes, n, parent_idx, upstream, kids);
    size_t extra_count = 0u;
    for (size_t j = 0u; j < n; ++j) {
        if (!node_is_ix(&nodes[j]))
            continue;
        if ((size_t)j == (size_t)upstream)
            continue;
        if ((size_t)parent_idx[j] == (size_t)upstream)
            continue;
        ++extra_count; /* nested or disconnected IX bridge */
    }

    if (child_count > (size_t)Y4_IX_DOWNSTREAM_MAX) {
        topo_reset(out);
        topo_stage(out, Y4_IX_TOPOLOGY_ERROR, -E2BIG);
        return -E2BIG;
    }

    /* Preserve the valid discovered roles for partial results too. */
    for (size_t k = 0u; k < child_count; ++k)
        out->downstream_index[k] = kids[k];
    out->downstream_count = (uint32_t)child_count;

    if (child_count == (size_t)Y4_IX_DOWNSTREAM_MAX && extra_count == 0u) {
        topo_stage(out, Y4_IX_TOPOLOGY_READY, 0);
        return 0;
    }

    topo_stage(out, Y4_IX_TOPOLOGY_PARTIAL, -EAGAIN);
    return -EAGAIN;
}
