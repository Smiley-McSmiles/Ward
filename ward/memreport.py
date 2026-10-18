"""Where Ward's memory goes: `ward --memory-report` asks the running Ward (SIGUSR1) to write a breakdown of its
memory, by mapping, thread, allocator and Python object, and prints it, with a short summary of Gajim when it runs,
for comparison.  Linux only (it reads /proc)."""

from __future__ import annotations

import collections
import ctypes
import gc
import os
import signal
import sys
import time

from . import paths


def report_file() -> str:
    return os.path.join(paths.ensure(paths.state_dir()), "ward-memory.txt")


def _rollup(pid: int | str) -> dict[str, int]:
    d = {}
    try:
        with open("/proc/%s/smaps_rollup" % pid) as f:
            for line in f:
                k, _, v = line.partition(":")
                if v.strip().endswith("kB"):
                    d[k] = int(v.split()[0])
    except OSError:
        pass
    return d


def _mappings(pid: int | str, top: int) -> list[str]:
    """The biggest mappings by private memory (what most monitors call an app's memory)."""
    rss: dict[str, int] = collections.Counter()
    private: dict[str, int] = collections.Counter()
    name = "[anon]"
    try:
        with open("/proc/%s/smaps" % pid) as f:
            for line in f:
                head = line.split(None, 5)
                if head and "-" in head[0] and not head[0].endswith(":"):
                    name = head[5].strip() if len(head) > 5 else "[anon]"
                elif line.startswith("Rss:"):
                    rss[name] += int(line.split()[1])
                elif line.startswith(("Private_Clean:", "Private_Dirty:")):
                    private[name] += int(line.split()[1])
    except OSError:
        return []
    return ["%9d kB private %9d kB resident  %s" % (private[n], rss[n], n)
            for n in sorted(private, key=lambda n: -private[n])[:top] if private[n] >= 1024]


def _cgroup_processes() -> list[str]:
    """Every process in our cgroup: monitors like Resources add them all up under the app."""
    try:
        with open("/proc/self/cgroup") as f:
            path = f.read().strip().split("::", 1)[-1]
        with open("/sys/fs/cgroup%s/cgroup.procs" % path) as f:
            pids = f.read().split()
    except OSError:
        return []
    out = ["cgroup %s" % path]
    for pid in pids:
        r = _rollup(pid)
        try:
            with open("/proc/%s/comm" % pid) as f:
                comm = f.read().strip()
        except OSError:
            continue
        out.append("  %7s %-20s resident %8d kB  anonymous %8d kB" % (pid, comm, r.get("Rss", 0),
                                                                       r.get("Anonymous", 0)))
    try:
        with open("/sys/fs/cgroup%s/memory.stat" % path) as f:
            stat = dict(line.split() for line in f)
        out.append("  cgroup memory: " + "  ".join("%s %d MB" % (k, int(stat[k]) // 2**20) for k in
                                                     ("anon", "file", "kernel", "shmem", "sock") if k in stat))
    except (OSError, ValueError):
        pass
    return out


class _Mallinfo2(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in ("arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks",
                                                 "fsmblks", "uordblks", "fordblks", "keepcost")]


def _malloc() -> str:
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.mallinfo2.restype = _Mallinfo2
        m = libc.mallinfo2()
    except (OSError, AttributeError):
        return "malloc: not glibc"
    mb = 2**20
    return "malloc: heap %d MB, in use %d MB, free but kept %d MB, large blocks %d MB" % (
        m.arena // mb, m.uordblks // mb, m.fordblks // mb, m.hblkhd // mb)


def build() -> str:
    """The report, from inside Ward."""
    lines = ["Ward memory report, %s, pid %d" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid())]
    try:
        import gi
        from gi.repository import Gtk

        lines.append("GTK %d.%d.%d, PyGObject %s, Python %s" % (Gtk.get_major_version(), Gtk.get_minor_version(),
                                                              Gtk.get_micro_version(), gi.__version__,
                                                              sys.version.split()[0]))
    except Exception:
        Gtk = None
    lines.append("environment: " + "  ".join("%s=%s" % (k, os.environ.get(k, "")) for k in (
        "GSK_RENDERER", "GDK_DISABLE", "GDK_DEBUG", "WARD_RENDERER", "XDG_SESSION_TYPE", "DRI_PRIME",
        "__NV_PRIME_RENDER_OFFLOAD")))
    r = _rollup("self")
    lines.append("resident %d MB, anonymous %d MB, private %d MB, swapped %d MB" % (
        r.get("Rss", 0) // 1024, r.get("Anonymous", 0) // 1024,
        (r.get("Private_Clean", 0) + r.get("Private_Dirty", 0)) // 1024, r.get("Swap", 0) // 1024))
    lines.append(_malloc())
    threads = collections.Counter()
    for t in os.listdir("/proc/self/task"):
        try:
            with open("/proc/self/task/%s/comm" % t) as f:
                threads[f.read().strip()] += 1
        except OSError:
            pass
    lines.append("threads %d: %s" % (sum(threads.values()), ", ".join(
        "%s x%d" % (n, c) if c > 1 else n for n, c in threads.most_common())))
    lines += ["", "biggest mappings:", *_mappings("self", 30)]
    lines += ["", *_cgroup_processes()]

    gc.collect()
    objs = gc.get_objects()
    types = collections.Counter(type(o).__name__ for o in objs)
    lines += ["", "Python: %d objects tracked, %d blocks allocated" % (len(objs), sys.getallocatedblocks()),
              "  " + ", ".join("%s %d" % t for t in types.most_common(25))]
    if Gtk is not None:
        widgets = collections.Counter(type(o).__name__ for o in objs if isinstance(o, Gtk.Widget))
        lines.append("widgets with Python wrappers: %d: %s" % (sum(widgets.values()), ", ".join(
            "%s %d" % t for t in widgets.most_common(15))))
        windows = [type(w).__name__ for w in Gtk.Window.list_toplevels()]
        lines.append("windows: %s" % ", ".join(windows))
        try:
            from .gui import widgets as ward_widgets

            tex = list(ward_widgets._textures.values())
            lines.append("decoded images cached: %d, %d MB" % (
                len(tex), sum(t.get_width() * t.get_height() * 4 for t in tex) // 2**20))
        except Exception:
            pass
    return "\n".join(lines) + "\n"


def write() -> bool:
    """SIGUSR1 handler in the running Ward."""
    try:
        text = build()
        tmp = report_file() + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, report_file())
    except Exception as e:
        print("memory report failed: %s" % e, file=sys.stderr)
    return True         # keep the handler


def _is(pid: str, name: str) -> bool:
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as f:
            argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return False
    if name == "ward":
        return any(os.path.basename(a) == "ward" for a in argv[:2]) or any(
            a == "-m" and argv[i + 1:i + 2] == ["ward"] for i, a in enumerate(argv))
    return any(name in os.path.basename(a).lower() for a in argv[:3])


def _mine() -> list[str]:
    me = os.getuid()
    out = []
    for pid in os.listdir("/proc"):
        if pid.isdigit() and int(pid) != os.getpid():
            try:
                if os.stat("/proc/" + pid).st_uid == me:
                    out.append(pid)
            except OSError:
                pass
    return out


def run() -> int:
    """`ward --memory-report`: from a terminal, while Ward runs."""
    pids = [p for p in _mine() if _is(p, "ward")]
    if not pids:
        print("Ward is not running.")
        return 1
    path = report_file()
    for pid in pids:
        before = os.path.getmtime(path) if os.path.exists(path) else 0
        os.kill(int(pid), signal.SIGUSR1)
        for _ in range(100):
            time.sleep(0.1)
            if os.path.exists(path) and os.path.getmtime(path) > before:
                break
        else:
            print("Ward (pid %s) did not answer: is it this version?" % pid)
            continue
        time.sleep(0.1)
        with open(path) as f:
            print(f.read())
    for pid in (p for p in _mine() if _is(p, "gajim")):
        r = _rollup(pid)
        print("For comparison, Gajim (pid %s): resident %d MB, anonymous %d MB" % (
            pid, r.get("Rss", 0) // 1024, r.get("Anonymous", 0) // 1024))
        print("\n".join(_mappings(pid, 12)))
    print("\n(also saved in %s)" % path)
    return 0
