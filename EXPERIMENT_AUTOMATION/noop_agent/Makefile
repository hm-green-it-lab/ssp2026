# Build the no-op eBPF control agent.
#
# Skeleton-free: the loader opens the object with bpf_object__open_file rather
# than going through `bpftool gen skeleton`. bpftool is still needed once, to
# dump the running kernel's BTF into vmlinux.h -- bpf/usdt.bpf.h needs the
# kernel types, and that header is what provides the USDT support maps libbpf
# populates at attach time.

CLANG   ?= clang
CC      ?= gcc
BPFTOOL ?= bpftool

BINARY  := ebpf-noop-agent
BPF_OBJ := noop.bpf.o
VMLINUX := vmlinux.h

# `-target bpf` drops the host include paths, so the <asm/*.h> headers that
# bpf/usdt.bpf.h reaches via <linux/errno.h> are not found. On Debian/Ubuntu
# they live in the multiarch directory; add it explicitly rather than
# symlinking asm-generic into /usr/include as some setups do. `-I.` comes
# first so our generated vmlinux.h wins.
ARCH_TRIPLET := $(shell dpkg-architecture -qDEB_HOST_MULTIARCH 2>/dev/null || echo $(shell uname -m)-linux-gnu)

CFLAGS     := -O2 -Wall
BPF_CFLAGS := -O2 -g -target bpf -D__TARGET_ARCH_x86 -I. -I/usr/include/$(ARCH_TRIPLET)
LDFLAGS    := -lbpf -lelf -lz

.PHONY: all clean

all: $(BINARY) $(BPF_OBJ)

# Generated from the running kernel, so it is never shipped from the controller.
$(VMLINUX):
	$(BPFTOOL) btf dump file /sys/kernel/btf/vmlinux format c > $@

$(BPF_OBJ): noop.bpf.c $(VMLINUX)
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@

$(BINARY): noop.c
	$(CC) $(CFLAGS) $< -o $@ $(LDFLAGS)

clean:
	rm -f $(BINARY) $(BPF_OBJ) $(VMLINUX)
