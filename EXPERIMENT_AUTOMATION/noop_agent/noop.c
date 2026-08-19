// noop.c
//
// User-space loader for the no-op eBPF agent.
//
// The command line deliberately mirrors the eBPF jAgent -- `-p <pid>`, an
// optional `-f <filter>` and an optional output path -- so the experiment
// automation can drive both through the same code path and only the binary
// differs. The filter and the output path are accepted and ignored: there is
// nothing to filter and nothing to record.
//
// JVM_LIB_PATH must point at the libjvm.so of the target JVM, exactly as for
// the jAgent, because that is the object whose USDT probe sites are patched.

#include <errno.h>
#include <getopt.h>
#include <limits.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <bpf/libbpf.h>

#define BPF_OBJECT_NAME "noop.bpf.o"

// The probes the eBPF jAgent attaches to. Keeping the set identical is the
// point: a different set would measure a different amount of trapping.
//
// `memory` marks the probe that --probes can switch off. The two method probes
// are always attached: they are the transaction boundary and the CPU source,
// and they are what the control is measuring the cost of.
static const struct {
    const char *program;
    const char *probe;
    bool optional_memory;
} PROBES[] = {
    {"noop_method_entry", "method__entry", false},
    {"noop_method_return", "method__return", false},
    {"noop_object_alloc", "object__alloc", true},
};

static volatile sig_atomic_t exiting = 0;

static void on_signal(int sig)
{
    (void)sig;
    exiting = 1;
}

// Keep libbpf quiet unless something is actually wrong.
static int libbpf_print(enum libbpf_print_level level, const char *format, va_list args)
{
    if (level == LIBBPF_DEBUG)
        return 0;
    return vfprintf(stderr, format, args);
}

// Locate noop.bpf.o next to this executable, so the agent can be started from
// any working directory.
static int object_path(char *out, size_t out_len)
{
    char exe[PATH_MAX];
    ssize_t len = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
    if (len < 0)
        return -1;
    exe[len] = '\0';

    char *slash = strrchr(exe, '/');
    if (!slash)
        return -1;
    *slash = '\0';

    if ((size_t)snprintf(out, out_len, "%s/%s", exe, BPF_OBJECT_NAME) >= out_len)
        return -1;
    return 0;
}

static void usage(const char *program)
{
    fprintf(stderr,
            "Usage: %s -p <java-pid> [-f <filter>] [--probes <list>] [output_file]\n"
            "  -p, --pid <java-pid>   PID of the Java process to attach to (required)\n"
            "  -f <filter>            accepted and ignored (jAgent compatibility)\n"
            "  --probes <list>        cpu,memory or all (default: all). Mirrors the jAgent\n"
            "                         flag so the control attaches the same probe set.\n"
            "                         network and storage are refused: this agent has no\n"
            "                         such programs, and ignoring them silently would make\n"
            "                         the control differ from what it controls for.\n"
            "  --min-duration-us <n>  accepted and ignored (jAgent compatibility): there is\n"
            "                         no emission to gate.\n"
            "  output_file            accepted and ignored (jAgent compatibility)\n"
            "  -h                     show this help and exit\n"
            "\n"
            "Attaches empty eBPF programs to the HotSpot method__entry,\n"
            "method__return and object__alloc USDT probes. JVM_LIB_PATH must name\n"
            "the libjvm.so of the target JVM.\n",
            program);
}

// Parse the jAgent-compatible dimension list. cpu is implicit in the method
// probes; memory maps to object__alloc; the remaining dimensions do not exist
// here and are refused rather than ignored.
static bool parse_probes(const char *spec, bool *memory)
{
    if (strcmp(spec, "all") == 0) {
        *memory = true;
        return true;
    }

    *memory = false;

    char buffer[128];
    snprintf(buffer, sizeof(buffer), "%s", spec);

    for (char *token = strtok(buffer, ","); token; token = strtok(NULL, ",")) {
        while (*token == ' ')
            token++;
        if (strcmp(token, "cpu") == 0)
            continue; // always on: it is the method probe itself
        else if (strcmp(token, "memory") == 0)
            *memory = true;
        else if (strcmp(token, "network") == 0 || strcmp(token, "storage") == 0) {
            fprintf(stderr,
                    "the no-op control agent has no %s probe; it exists to isolate the "
                    "cost of the method and allocation probes only\n", token);
            return false;
        } else {
            fprintf(stderr, "unknown probe dimension: %s\n", token);
            return false;
        }
    }
    return true;
}

int main(int argc, char **argv)
{
    pid_t target_pid = 0;
    bool memory = true;
    int opt;
    int long_index = 0;

    static struct option long_opts[] = {
        {"pid", required_argument, NULL, 'p'},
        {"probes", required_argument, NULL, 0},
        // Accepted and ignored: there is no emission to gate. It has to be
        // accepted rather than rejected because the automation drives this
        // agent and the jAgent through one code path, and getopt would refuse
        // the unknown option and abort the control run.
        {"min-duration-us", required_argument, NULL, 0},
        {"help", no_argument, NULL, 'h'},
        {0, 0, 0, 0},
    };

    while ((opt = getopt_long(argc, argv, "p:f:h", long_opts, &long_index)) != -1) {
        switch (opt) {
        case 0:
            if (strcmp(long_opts[long_index].name, "probes") == 0 &&
                !parse_probes(optarg, &memory)) {
                return 1;
            }
            break;
        case 'p': {
            char *end = NULL;
            long value = strtol(optarg, &end, 10);
            if (end == optarg || *end != '\0' || value <= 0) {
                fprintf(stderr, "invalid java-pid: %s\n", optarg);
                return 1;
            }
            target_pid = (pid_t)value;
            break;
        }
        case 'f':
            break; // accepted for jAgent compatibility, intentionally unused
        case 'h':
            usage(argv[0]);
            return 0;
        default:
            usage(argv[0]);
            return 1;
        }
    }

    if (target_pid == 0) {
        fprintf(stderr, "error: missing required -p/--pid argument\n");
        usage(argv[0]);
        return 1;
    }

    const char *jvm_lib_path = getenv("JVM_LIB_PATH");
    if (!jvm_lib_path || jvm_lib_path[0] == '\0') {
        fprintf(stderr, "JVM_LIB_PATH not set; cannot resolve the HotSpot USDT probes\n");
        return 1;
    }

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    libbpf_set_print(libbpf_print);

    char path[PATH_MAX];
    if (object_path(path, sizeof(path)) != 0) {
        fprintf(stderr, "could not locate %s next to the executable\n", BPF_OBJECT_NAME);
        return 1;
    }

    struct bpf_object *obj = bpf_object__open_file(path, NULL);
    if (!obj) {
        fprintf(stderr, "failed to open %s: %s\n", path, strerror(errno));
        return 1;
    }

    if (bpf_object__load(obj)) {
        fprintf(stderr, "failed to load BPF object: %s\n", strerror(errno));
        bpf_object__close(obj);
        return 1;
    }

    struct bpf_link *links[sizeof(PROBES) / sizeof(PROBES[0])] = {0};
    size_t attached = 0;

    size_t wanted = 0;
    for (size_t i = 0; i < sizeof(PROBES) / sizeof(PROBES[0]); i++) {
        if (PROBES[i].optional_memory && !memory)
            continue;
        wanted++;

        struct bpf_program *prog = bpf_object__find_program_by_name(obj, PROBES[i].program);
        if (!prog) {
            fprintf(stderr, "failed to attach: no program named %s\n", PROBES[i].program);
            goto cleanup;
        }

        links[i] = bpf_program__attach_usdt(prog, target_pid, jvm_lib_path,
                                            "hotspot", PROBES[i].probe, NULL);
        if (!links[i]) {
            fprintf(stderr, "failed to attach USDT hotspot:%s to pid %d (%s)\n",
                    PROBES[i].probe, target_pid, strerror(errno));
            goto cleanup;
        }
        attached++;
        printf("attached hotspot:%s\n", PROBES[i].probe);
    }

    printf("noop agent attached to PID %d (%zu probes); doing nothing until interrupted\n",
           target_pid, attached);
    fflush(stdout);

    while (!exiting)
        sleep(1);

    printf("interrupted; detaching\n");

cleanup:
    for (size_t i = 0; i < sizeof(links) / sizeof(links[0]); i++) {
        if (links[i])
            bpf_link__destroy(links[i]);
    }
    bpf_object__close(obj);
    return attached == wanted ? 0 : 1;
}
