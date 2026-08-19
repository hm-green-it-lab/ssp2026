"""
sut_setup.py

One-off preparation of the system under test. Run this once per SUT before using
``main.py``; everything that changes between runs -- the jAgent build, the OTJAE
JARs, the test plan -- is staged by the automation itself and does not belong
here.

The script connects over SSH using the credentials in ``../.env`` and the hosts
and paths in ``../paths.env``, then:

1. checks the OS and the tools the experiments need;
2. installs the missing apt packages (the jAgent's runtime libraries, binutils,
   curl, git) unless ``--check-only``;
3. verifies passwordless sudo, BTF, and that ``JVM_LIB_PATH`` actually carries
   the HotSpot USDT probes -- the one prerequisite whose absence fails silently
   at measurement time rather than loudly at setup time;
4. creates ``${SUT_BASE_DIR}/work``;
5. clones the RETIT repository at a pinned tag and builds
   ``spring-rest-service.jar``, then places it at ``${SPRING_REST_SERVICE_JAR}``.

There is deliberately **no container** built here. The application runs as a
plain ``java -jar`` process, and the only container in this setup is the
OpenTelemetry Collector, which runs on the *controller* rather than the SUT so it
cannot compete for the CPU cycles being measured.

The SUT needs outbound internet access for steps 2 and 5.

Usage::

    python setup/sut_setup.py                 # check, install, build
    python setup/sut_setup.py --check-only    # report only, change nothing
    python setup/sut_setup.py --tag v0.1.1-beta
    python setup/sut_setup.py --host 10.0.0.99 --force-rebuild
"""

from __future__ import annotations

import argparse
import posixpath
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(dotenv_path=HERE / ".env")

from helper.jagent import check_usdt_probes  # noqa: E402
from helper.ssh import (  # noqa: E402
    connect,
    credentials,
    ensure_dir,
    exists,
    has_passwordless_sudo,
    is_root,
    privilege_prefix,
    run,
)

# Match the extension JAR the automation downloads, so the example application
# and the agent that instruments it come from the same release.
DEFAULT_RETIT_TAG = "v0.1.1-beta"
RETIT_REPO = "https://github.com/RETIT/opentelemetry-javaagent-extension.git"

# The full reactor, deliberately. Narrowing to `-pl examples/spring-rest-service
# -am` does not work: that module pulls io.retit:extension in through
# dependency-plugin artifact items rather than a Maven dependency, so `-am`
# cannot see it and the build fails to resolve extension:0.0.1-SNAPSHOT.
# Override with --module if a future layout makes narrowing possible.
DEFAULT_MAVEN_MODULE = ""

# Every example module binds jib-maven-plugin to `package` to produce a
# container image. The SUT has no Docker daemon -- deliberately, since the
# application is run as a plain `java -jar` process -- so jib must be skipped or
# the build dies before the JAR is produced.
MAVEN_FLAGS = ["-B", "-DskipTests", "-Djib.skip=true"]

# Runtime libraries the published jAgent binary links against, plus the tools
# the automation shells out to on the SUT.
APT_PACKAGES = [
    "libbpf1",
    "libelf1",
    "zlib1g",
    "libcurl4",
    "binutils",   # readelf, for the USDT probe check
    "curl",       # warm-up requests
    "git",
    "maven",      # falls back to ./mvnw if the repo ships one
    # Needed only by the `noop` control agent, which is compiled on the SUT.
    # The published jAgent release needs no toolchain.
    "make",
    "clang",
    "gcc",
    "bpftool",
    "libbpf-dev",
    "libcurl4-openssl-dev",   # jAgent source build needs the curl headers
    "libelf-dev",
    "zlib1g-dev",
    "linux-libc-dev",
]

# command -> human name
TOOL_CHECKS = {
    "git": "git",
    "curl": "curl",
    "readelf": "readelf (binutils)",
    "tar": "tar",
    "ss": "ss (iproute2)",
}


def read_env_file(path: Path) -> dict:
    """Parse a ``KEY=VALUE`` env file into a dict."""
    values: dict[str, str] = {}
    if not path.exists():
        raise FileNotFoundError(f"{path} not found (copy the .template first)")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def report_os(client) -> str:
    """Print the SUT's distribution and kernel; return the OS id."""
    _, out, _ = run(client, ". /etc/os-release && echo \"$ID|$VERSION_ID|$PRETTY_NAME\"", check=False)
    os_id, _, pretty = (out.strip().split("|", 2) + ["", "", ""])[:3]
    _, kernel, _ = run(client, "uname -r", check=False)
    print(f"[~] SUT: {pretty or 'unknown'} | kernel {kernel.strip()}")
    if os_id not in {"debian", "ubuntu"}:
        print(
            f"[!] WARNING: expected a Debian/Ubuntu SUT (got {os_id!r}); "
            "the apt steps below will not apply"
        )
    return os_id


def check_tools(client) -> list[str]:
    """Report which expected commands are present; return the missing ones."""
    print("[~] Checking tools ...")
    missing = []
    for command, name in TOOL_CHECKS.items():
        code, _, _ = run(client, f"command -v {command}", check=False)
        print(f"    {'[v]' if code == 0 else '[x]'} {name}")
        if code != 0:
            missing.append(command)
    return missing


def check_java(client, java_bin: str) -> None:
    """Verify the configured JDK exists and report its version."""
    print("[~] Checking Java ...")
    if not exists(client, java_bin):
        raise RuntimeError(
            f"[x] JAVA_BIN does not exist on the SUT: {java_bin}\n"
            "    Install a JDK whose openjdk build ships the DTrace probes, e.g.\n"
            "      sudo apt install -y openjdk-25-jdk"
        )
    _, out, err = run(client, f"{shlex.quote(java_bin)} -version 2>&1 | head -n 1", check=False)
    print(f"    [v] {(out or err).strip()}")


def check_privileges(client) -> None:
    """Verify the session can gain root, and that the kernel exposes BTF.

    Connecting as root is fine and common on a minimal Debian or Proxmox
    install, where ``sudo`` is often not installed at all -- in that case there
    is nothing to check and nothing to prefix.
    """
    print("[~] Checking privileges and kernel ...")
    if is_root(client):
        print("    [v] connected as root")
    elif has_passwordless_sudo(client):
        print("    [v] passwordless sudo")
    else:
        raise RuntimeError(
            "[x] root privileges are required to load the eBPF programs, but the "
            "SSH user is not root and passwordless sudo is unavailable.\n"
            "    Either connect as root, or grant sudo:\n"
            "      echo \"$USER ALL=(ALL) NOPASSWD: ALL\" | sudo tee /etc/sudoers.d/ebpf-experiments"
        )

    code, _, _ = run(client, "test -r /sys/kernel/btf/vmlinux", check=False)
    if code != 0:
        print(
            "    [x] /sys/kernel/btf/vmlinux not readable — the agent needs a "
            "BTF-enabled kernel (CONFIG_DEBUG_INFO_BTF). On Proxmox this usually "
            "means the SUT is an LXC container rather than a VM."
        )
    else:
        print("    [v] BTF available")


def check_port_free(client, port: int = 8081) -> None:
    """Warn if something already owns the application port."""
    _, out, _ = run(
        client,
        f"(ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -E '[:.]{port}\\b' || true",
        check=False,
    )
    if out.strip():
        print(f"[!] WARNING: port {port} is currently in use:\n{out.strip()}")
    else:
        print(f"[~] Port {port} is free")


def install_packages(client, check_only: bool) -> None:
    """Install the apt packages the runs depend on."""
    if check_only:
        print(f"[~] --check-only: would install {' '.join(APT_PACKAGES)}")
        return

    print(f"[~] Installing apt packages: {' '.join(APT_PACKAGES)}")
    # Empty when the session is already root, which is the usual case here.
    sudo = privilege_prefix(client)
    run(client, f"{sudo}apt-get update -qq", timeout=900)
    # Package names differ across releases; report rather than abort so a single
    # unavailable name does not block an otherwise usable SUT.
    code, out, err = run(
        client,
        f"{sudo}env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        + " ".join(APT_PACKAGES),
        check=False,
        timeout=1800,
    )
    if code != 0:
        print(f"[!] apt-get reported errors (continuing):\n{(out + err).strip()[-1500:]}")
    else:
        print("    [v] packages installed")


SERVICE_JAVA = "examples/spring-rest-service/src/main/java/io/retit/spring/carbon/TestService.java"
SERVICE_POM = "examples/spring-rest-service/pom.xml"

# Version is omitted deliberately: the root pom's dependencyManagement pins it,
# and examples/simple-jdk8-application already declares it exactly this way.
ANNOTATIONS_DEPENDENCY = """\t\t<dependency>
\t\t\t<groupId>io.opentelemetry.instrumentation</groupId>
\t\t\t<artifactId>opentelemetry-instrumentation-annotations</artifactId>
\t\t</dependency>
"""


def _read_remote(client, path: str) -> str:
    """Read a remote text file over SFTP."""
    sftp = client.open_sftp()
    try:
        with sftp.open(path, "r") as handle:
            return handle.read().decode("utf-8")
    finally:
        sftp.close()


def _write_remote(client, path: str, content: str) -> None:
    """Write a remote text file over SFTP."""
    sftp = client.open_sftp()
    try:
        with sftp.open(path, "w") as handle:
            handle.write(content.encode("utf-8"))
    finally:
        sftp.close()


def patch_spring_service(client, repo_dir: str, check_only: bool) -> bool:
    """Annotate ``veryComplexBusinessFunction`` with ``@WithSpan``.

    Without this, OTJAE only produces spans at the HTTP request boundary while
    the eBPF jAgent measures the business method, so the two tools report
    different populations and their CPU and memory figures are not comparable.
    The annotation makes the OpenTelemetry agent emit a span for exactly the
    method the jAgent filters on.

    Applied here rather than by hand on the SUT so that it survives
    ``--force-rebuild``: a silently reverted patch would change what the
    comparison means without any visible symptom.

    Returns True when a change was made. Idempotent.
    """
    java_path = f"{repo_dir}/{SERVICE_JAVA}"
    pom_path = f"{repo_dir}/{SERVICE_POM}"

    java = _read_remote(client, java_path)
    pom = _read_remote(client, pom_path)

    needs_java = "@WithSpan" not in java
    needs_pom = "opentelemetry-instrumentation-annotations" not in pom

    if not needs_java and not needs_pom:
        print("[~] @WithSpan annotation already present; nothing to patch")
        return False
    if check_only:
        print("[~] --check-only: would annotate veryComplexBusinessFunction with @WithSpan")
        return False

    if needs_pom:
        anchor = "\t<dependencies>\n"
        if anchor not in pom:
            raise RuntimeError(f"[x] could not find <dependencies> in {pom_path}")
        pom = pom.replace(anchor, anchor + ANNOTATIONS_DEPENDENCY, 1)
        _write_remote(client, pom_path, pom)
        print("    [v] added opentelemetry-instrumentation-annotations to the POM")

    if needs_java:
        import_anchor = "import org.springframework.stereotype.Service;\n"
        if import_anchor not in java:
            raise RuntimeError(f"[x] could not find the Service import in {java_path}")
        java = java.replace(
            import_anchor,
            "import io.opentelemetry.instrumentation.annotations.WithSpan;\n" + import_anchor,
            1,
        )

        method_anchor = "    public String veryComplexBusinessFunction(final int size)"
        if method_anchor not in java:
            raise RuntimeError(f"[x] could not find veryComplexBusinessFunction in {java_path}")
        java = java.replace(method_anchor, "    @WithSpan\n" + method_anchor, 1)

        _write_remote(client, java_path, java)
        print("    [v] annotated veryComplexBusinessFunction with @WithSpan")

    return True


def build_spring_service(
    client,
    base_dir: str,
    jar_target: str,
    tag: str,
    force_rebuild: bool,
    check_only: bool,
    module: str = DEFAULT_MAVEN_MODULE,
) -> None:
    """Clone the RETIT repository at *tag* and build the Spring service JAR.

    The example application has no published release -- the RETIT releases carry
    only the extension JAR -- so it has to be built from source on the SUT.
    """
    repo_dir_probe = f"{base_dir}/src/otjae"
    already_patched = exists(client, repo_dir_probe) and "@WithSpan" in _read_remote(
        client, f"{repo_dir_probe}/{SERVICE_JAVA}"
    ) if exists(client, f"{repo_dir_probe}/{SERVICE_JAVA}") else False

    if exists(client, jar_target) and not force_rebuild and already_patched:
        print(f"[~] {jar_target} already present and patched; skipping build "
              "(--force-rebuild to redo)")
        return
    if exists(client, jar_target) and not force_rebuild and not already_patched:
        print("[~] jar exists but the @WithSpan patch is not applied; rebuilding")
    if check_only:
        print(
            f"[~] --check-only: would clone {RETIT_REPO} @ {tag}, build "
            f"{module or 'the full reactor'} and install {jar_target}"
        )
        return

    src_dir = f"{base_dir}/src"
    repo_dir = f"{src_dir}/otjae"
    ensure_dir(client, src_dir)

    if exists(client, f"{repo_dir}/.git"):
        print(f"[~] Repository present; fetching tags ...")
        run(client, f"cd {shlex.quote(repo_dir)} && git fetch --all --tags --quiet", timeout=900)
    else:
        print(f"[~] Cloning {RETIT_REPO} ...")
        run(client, f"git clone --quiet {RETIT_REPO} {shlex.quote(repo_dir)}", timeout=1800)

    print(f"[~] Checking out {tag} ...")
    run(client, f"cd {shlex.quote(repo_dir)} && git checkout --quiet tags/{shlex.quote(tag)}")

    # git checkout restores the pristine sources, so the annotation must be
    # re-applied on every checkout, not only on the first clone.
    patch_spring_service(client, repo_dir, check_only=False)

    flags = " ".join(MAVEN_FLAGS)
    selector = f"-pl {shlex.quote(module)} -am" if module else ""
    # Prefer the wrapper so the build uses the Maven version the project expects.
    if exists(client, f"{repo_dir}/mvnw"):
        build = f"chmod +x ./mvnw && ./mvnw {flags} {selector} package"
    else:
        build = f"mvn {flags} {selector} package"

    print("[~] Building (this takes a few minutes) ...")
    # Maven's output goes to a file and is tailed separately, so the exit code
    # reported back is Maven's own. Piping it straight into `tail` would yield
    # tail's exit code instead, and a failed build would look successful.
    log = f"{repo_dir}/.build.log"
    code, out, err = run(
        client,
        f"cd {shlex.quote(repo_dir)} && {{ {build}; }} > {shlex.quote(log)} 2>&1; "
        f"rc=$?; tail -n 30 {shlex.quote(log)}; exit $rc",
        check=False,
        timeout=3600,
    )
    tail = (out + err).strip()
    if tail:
        print("    build output (tail):\n" + tail)
    if code != 0:
        raise RuntimeError(
            f"[x] Maven build failed (exit {code}); full log on the SUT at {log}.\n"
            "    If it is a JDK compatibility error, build with an older JDK via "
            "JAVA_HOME — only the *runtime* JDK needs the DTrace probes."
        )

    # Locate the artifact rather than hardcoding the module layout.
    _, found, _ = run(
        client,
        f"find {shlex.quote(repo_dir)} -name 'spring-rest-service*.jar' "
        f"-not -name '*sources*' -not -name '*javadoc*' | head -n 1",
        check=False,
    )
    built = found.strip()
    if not built:
        raise RuntimeError(
            f"[x] build produced no spring-rest-service jar under {repo_dir}"
        )
    print(f"    [v] built {built}")

    # posixpath, not pathlib: this is a remote POSIX path being manipulated on a
    # controller that may well be Windows.
    ensure_dir(client, posixpath.dirname(jar_target) or ".")
    run(client, f"cp -f {shlex.quote(built)} {shlex.quote(jar_target)}")
    print(f"    [v] installed to {jar_target}")


def main() -> None:
    """Run the full SUT preparation."""
    parser = argparse.ArgumentParser(description="Prepare the SUT for the experiment runs.")
    parser.add_argument("--host", help="Override SUT_HOST from paths.env")
    parser.add_argument(
        "--check-only", action="store_true", help="Report only; install and build nothing"
    )
    parser.add_argument("--tag", default=DEFAULT_RETIT_TAG, help="RETIT repository tag to build")
    parser.add_argument(
        "--skip-build", action="store_true", help="Do not build the Spring service"
    )
    parser.add_argument(
        "--force-rebuild", action="store_true", help="Rebuild even if the JAR exists"
    )
    parser.add_argument(
        "--module",
        default=DEFAULT_MAVEN_MODULE,
        help="Maven module to build (empty string builds the full reactor)",
    )
    args = parser.parse_args()

    paths = read_env_file(HERE / "paths.env")
    host = args.host or paths["SUT_HOST"]
    base_dir = paths["SUT_BASE_DIR"].rstrip("/")
    java_bin = paths["JAVA_BIN"]
    jvm_lib_path = paths["JVM_LIB_PATH"]
    jar_target = paths["SPRING_REST_SERVICE_JAR"]

    user, password = credentials("SUT")
    print(f"[~] Connecting to {user}@{host} ...")
    client = connect(host, user, password)

    try:
        _, out, _ = run(client, "echo CONNECTED")
        if out.strip() != "CONNECTED":
            raise RuntimeError("SSH login failed (unexpected response)")
        print("[v] SSH connection established")

        report_os(client)
        missing = check_tools(client)
        if missing and args.check_only:
            print(f"[!] Missing tools: {', '.join(missing)} (--check-only, not installing)")

        install_packages(client, args.check_only)

        if not args.check_only:
            still_missing = check_tools(client)
            if still_missing:
                print(f"[!] Still missing after install: {', '.join(still_missing)}")

        check_java(client, java_bin)
        check_privileges(client)

        # The decisive check: a JDK without the HotSpot probes runs fine and
        # produces an empty trace, which is indistinguishable from "no
        # transactions found" once an experiment is under way.
        print("[~] Checking the HotSpot USDT probes in libjvm.so ...")
        if exists(client, jvm_lib_path):
            check_usdt_probes(client, jvm_lib_path)
        else:
            raise RuntimeError(f"[x] JVM_LIB_PATH does not exist on the SUT: {jvm_lib_path}")

        check_port_free(client)

        work_dir = f"{base_dir}/work"
        if not args.check_only:
            ensure_dir(client, work_dir)
            print(f"[v] Work directory ready: {work_dir}")

        if args.skip_build:
            print("[~] --skip-build: leaving the Spring service alone")
        else:
            build_spring_service(
                client,
                base_dir,
                jar_target,
                args.tag,
                args.force_rebuild,
                args.check_only,
                module=args.module,
            )

        print(
            "\n[v] SUT preparation complete.\n"
            f"    SPRING_REST_SERVICE_JAR = {jar_target}\n"
            f"    JAVA_BIN                = {java_bin}\n"
            f"    JVM_LIB_PATH            = {jvm_lib_path}\n"
            "\nThe eBPF jAgent is not installed here — the automation ships the\n"
            "published release to the SUT on every run. Next:\n"
            "    python -m main --config configuration/spring_remote_none.yml --dry-run"
        )

    finally:
        client.close()


if __name__ == "__main__":
    main()
