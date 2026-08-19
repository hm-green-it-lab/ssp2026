// noop.bpf.c
//
// The control for the overhead experiment: eBPF programs attached to exactly
// the same HotSpot USDT probes as the eBPF jAgent, doing nothing at all.
//
// The difference between this configuration and "USDT only" is the cost of the
// probe *firing*: with no consumer attached, the JVM's DTrace probe sites stay
// nop-patched and are nearly free, whereas attaching any BPF program turns them
// into traps, so every Java method entry and return becomes a user->kernel
// transition. The difference between this and the full jAgent is then the cost
// of the agent's own bookkeeping.
//
// Deliberately no maps, no arguments read, no helpers called -- anything else
// would contaminate the measurement this configuration exists to make.

// bpf/usdt.bpf.h defines the support maps libbpf populates when attaching a
// USDT probe; without it bpf_program__attach_usdt() fails with "failed to find
// USDT support BPF maps". It needs the kernel types, hence vmlinux.h, which the
// Makefile generates on the SUT from the running kernel's BTF.
#include "vmlinux.h"

#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/usdt.bpf.h>

SEC("usdt")
int noop_method_entry(struct pt_regs *ctx)
{
    return 0;
}

SEC("usdt")
int noop_method_return(struct pt_regs *ctx)
{
    return 0;
}

SEC("usdt")
int noop_object_alloc(struct pt_regs *ctx)
{
    return 0;
}

char LICENSE[] SEC("license") = "Dual BSD/GPL";
