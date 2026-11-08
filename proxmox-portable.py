#!/usr/bin/env python3
"""proxmox-portable - run Proxmox VE guests on a standalone Debian machine, and move them back.

One script, run as root on the portable machine (a laptop, a spare box). It is:
  * a Proxmox-style web console on port 8006 (HTTPS, username/password from the config file),
  * the hypervisor side: it runs moved VMs under Proxmox's own QEMU with the exact command line
    Proxmox builds (`qm showcmd`), and moved containers under LXC,
  * the mover: live or cold VM moves and restart-mode container moves out of the cluster and back,
    with changed-block syncs back to the parked copy on the home node in between.

It talks to the cluster in two ways:
  * the Proxmox API (an API token, or `pvesh` over SSH when no token is set) for the console views,
  * root SSH to the nodes for disk streams, QMP on the source QEMU and the locked write-back.

Usage:  proxmox-portable [--config FILE] COMMAND [ARGS]
  serve                     web console + scheduler (what the systemd unit runs)
  setup                     install Debian packages, directories, SSH key, systemd unit
  fetch-qemu [NODE_IP]      copy Proxmox's QEMU/swtpm/OVMF from a node into the bundle
  check                     check config, cluster access, SSH and the QEMU bundle
  hash-password             print a password_hash line for the config file
  status | list | vm-status ID | jobs | job ID | history [ID]
  start|shutdown|stop|reboot ID
  sync ID | set-sync ID INTERVAL|off | net-up | boot
Internal: _run-job ID | ifup TAP | ifdown TAP

Config file: /etc/proxmox-portable/proxmox-portable.conf (see proxmox-portable.conf.example).
"""
import configparser
import fcntl
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
DEFAULT_CONF = "/etc/proxmox-portable/proxmox-portable.conf"
SELF = os.path.realpath(__file__)
QRUN = "/var/run/qemu-server"          # Proxmox's command lines expect their sockets here
LXCPATH = "/var/lib/lxc"
QIMG = "qemu-img"
FLAG_OPTS = {"-nodefaults", "-daemonize", "-no-shutdown", "-no-user-config", "-S", "-no-hpet", "-enable-kvm", "-no-reboot"}
INTERVALS = {"15min": 900, "30min": 1800, "hourly": 3600, "2h": 7200, "6h": 21600, "daily": 86400}
REMOTE_TMP = "/var/tmp/proxmox-portable"
# Debian packages the script needs (setup installs them). The Proxmox QEMU bundle needs the libraries.
DEB_PKGS = ("qemu-utils dnsmasq-base zstd python3 rsync nftables iptables lxc fuse3 openssl openssh-client "
            "iproute2 libaio1t64 libasound2t64 libcap-ng0 libcurl3t64-gnutls libepoxy0 libfdt1 libfuse3-4 libgbm1 "
            "libglib2.0-0t64 libgnutls30t64 libiscsi7 libjpeg62-turbo libnuma1 libpixman-1-0 libpng16-16t64 "
            "libpulse0 libseccomp2 libslirp0 libsndio7.0 libspice-server1 liburing2 libusb-1.0-0 "
            "libusbredirparser1t64 libvirglrenderer1 libxkbcommon0 libibverbs1 liblttng-ust1t64 librdmacm1t64 "
            "libnbd0 libcryptsetup12 libjson-glib-1.0-0").split()
PVE_DEBS = ("pve-qemu-kvm libproxmox-backup-qemu0 pve-edk2-firmware-ovmf pve-edk2-firmware-legacy librados2 "
            "librbd1 swtpm swtpm-libs libtpms0").split()


class UserError(Exception):
    """An error with a message meant for the operator (no traceback)."""


# ================================================================ configuration
class C:
    """Settings from the config file (filled by load_config)."""
    path = DEFAULT_CONF


def _list(s):
    return [x for x in re.split(r"[,\s]+", s or "") if x]


def load_config(path):
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=None)
    if not os.path.exists(path):
        raise UserError("config file %s not found; copy proxmox-portable.conf.example there and edit it" % path)
    cp.read(path)
    g = lambda s, k, d="": cp.get(s, k, fallback=d).strip()
    C.path = path
    C.etc = os.path.dirname(os.path.abspath(path))
    # [web]
    C.listen = g("web", "listen", "0.0.0.0")
    C.port = int(g("web", "port", "8006"))
    C.username = g("web", "username", "admin")
    C.password = cp.get("web", "password", fallback="")
    C.password_hash = g("web", "password_hash")
    C.tls = cp.getboolean("web", "tls", fallback=True)
    C.tls_cert = g("web", "tls_cert", C.etc + "/tls.crt")
    C.tls_key = g("web", "tls_key", C.etc + "/tls.key")
    C.session_hours = float(g("web", "session_hours", "12"))
    C.allowed = [ipaddress.ip_network(x, strict=False) for x in _list(g("web", "allowed_networks"))]
    # [proxmox]
    C.pve_hosts = _list(g("proxmox", "hosts"))
    C.token = g("proxmox", "token")
    C.verify_tls = cp.getboolean("proxmox", "verify_tls", fallback=False)
    C.ssh_key = g("proxmox", "ssh_key", C.etc + "/id_pve")
    C.ssh_known = g("proxmox", "ssh_known_hosts", C.etc + "/pve_known_hosts")
    C.lan_bridges = _list(g("proxmox", "lan_bridges", "vmbr0"))
    # [host]
    C.host_name = g("host", "name") or socket.gethostname().split(".")[0]
    C.tag_prefix = g("host", "tag_prefix", "portable-")
    # [network]
    C.gateway = g("network", "gateway")
    C.prefix = int(g("network", "prefix", "24"))
    C.dns = _list(g("network", "dns"))
    C.lan_iface = g("network", "lan_iface")
    C.bridge = g("network", "bridge", "ppbr0")
    C.nat_bridge = g("network", "nat_bridge", "ppnat0")
    C.nat_net = ipaddress.ip_interface(g("network", "nat_network", "10.77.0.1/24"))
    # [paths]
    C.data = g("paths", "data_dir", "/var/lib/proxmox-portable")
    C.pveq = g("paths", "qemu_bundle", "/opt/proxmox-portable/pve-qemu")
    # [defaults]
    C.shutdown_timeout = int(g("defaults", "shutdown_timeout", "180"))
    C.sync_default = g("defaults", "sync_interval", "hourly")
    # derived
    C.vms, C.jobs, C.rrd = C.data + "/vms", C.data + "/jobs", C.data + "/rrd"
    C.helpers = C.data + "/bin"
    C.run = "/run/proxmox-portable"
    C.kvm, C.swtpm = C.pveq + "/bin/kvm", C.pveq + "/bin/swtpm"
    return C


def config_problems():
    p = []
    if not C.pve_hosts:
        p.append("[proxmox] hosts is empty")
    if not (C.password or C.password_hash):
        p.append("[web] needs password or password_hash")
    if C.password in ("change-me", "CHANGE-ME") and not C.password_hash:
        p.append("[web] password is still the example value 'change-me'")
    if C.token and not re.match(r"^[^@\s]+@[^!\s]+![^=\s]+=\S+$", C.token):
        p.append("[proxmox] token must look like user@realm!tokenid=secret")
    if not re.match(r"^[a-z0-9][a-z0-9_.+-]*$", C.tag_prefix + "x"):
        p.append("[host] tag_prefix may hold only a-z 0-9 _ . + -")
    if C.gateway and not re.match(r"^\d+\.\d+\.\d+\.\d+$", C.gateway):
        p.append("[network] gateway must be an IPv4 address")
    return p


# ================================================================ helpers
def run(cmd, check=True, input=None, timeout=None):
    p = subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError("%s -> rc %d: %s" % (" ".join(cmd)[:300], p.returncode,
                                                (p.stderr or p.stdout).strip()[-800:]))
    return p.stdout


def readf(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def write_json(path, obj):
    tmp = "%s.tmp%d" % (path, threading.get_ident())
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, default=str)
    os.replace(tmp, path)


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def vmdir(vmid):
    return "%s/%d" % (C.vms, int(vmid))


def load_meta(vmid):
    m = load_json(vmdir(vmid) + "/meta.json")
    if not m:
        raise UserError("guest %s is not managed on this host" % vmid)
    return m


def save_meta(m):
    write_json(vmdir(m["vmid"]) + "/meta.json", m)


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def stable_read(path):
    """Read a small file a running process may be rewriting; return it once two reads agree."""
    b = b""
    for _ in range(10):
        a = open(path, "rb").read()
        time.sleep(0.2)
        b = open(path, "rb").read()
        if a == b:
            return a
    return b


class Log:
    def __init__(self, path=None):
        self.path = path

    def __call__(self, msg):
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
        if self.path:
            with open(self.path, "a") as f:
                f.write(line + "\n")
        print(line, flush=True)


def my_tag():
    return C.tag_prefix + re.sub(r"[^a-z0-9-]", "-", C.host_name.lower())


def tag_list(tags):
    return [t for t in re.split(r"[;, ]+", tags or "") if t]


def parked_on(tags):
    """Name of the portable host a Proxmox guest is parked for (from its tag), or None."""
    for t in tag_list(tags):
        if t.startswith(C.tag_prefix) and len(t) > len(C.tag_prefix):
            return t[len(C.tag_prefix):]
    return None


# ================================================================ QMP / QGA
def _connect(path, timeout):
    last = None
    for _ in range(40):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(path)
            return s
        except OSError as e:
            last = e
            time.sleep(0.25)
    raise RuntimeError("cannot connect to %s: %s" % (path, last))


def qmp(path, cmd, args=None, timeout=60):
    """One-shot QMP command (connect, negotiate, run, disconnect)."""
    s = _connect(path, timeout)
    f = s.makefile("rwb", buffering=0)
    try:
        def rd():
            while True:
                line = f.readline()
                if not line:
                    raise RuntimeError("QMP connection closed")
                msg = json.loads(line)
                if "event" not in msg:
                    return msg
        rd()
        f.write(b'{"execute":"qmp_capabilities"}\n')
        rd()
        req = {"execute": cmd}
        if args:
            req["arguments"] = args
        f.write(json.dumps(req).encode() + b"\n")
        r = rd()
        if "error" in r:
            raise RuntimeError("QMP %s: %s" % (cmd, r["error"].get("desc")))
        return r.get("return")
    finally:
        s.close()


def qga(path, cmd, args=None, timeout=20):
    """One-shot guest-agent command over its chardev socket."""
    s = _connect(path, timeout)
    f = s.makefile("rwb", buffering=0)
    try:
        sid = int(time.time() * 1000) % 2**31
        s.settimeout(min(timeout, 4))     # no agent in the guest: fail fast on the handshake
        f.write(json.dumps({"execute": "guest-sync", "arguments": {"id": sid}}).encode() + b"\n")
        while True:
            line = f.readline()
            if not line:
                raise RuntimeError("guest agent closed")
            if json.loads(line).get("return") == sid:
                break
        s.settimeout(timeout)
        req = {"execute": cmd}
        if args:
            req["arguments"] = args
        f.write(json.dumps(req).encode() + b"\n")
        r = json.loads(f.readline())
        if "error" in r:
            raise RuntimeError("QGA %s: %s" % (cmd, r["error"].get("desc")))
        return r.get("return")
    finally:
        s.close()


# QMP on a Proxmox node: the same one-shot client, run there with python3. The node's QMP socket is
# shared with pvestatd, so every call is its own short connection.
PVE_QMP_PY = r'''
import json,socket,sys,time
vmid,cmd,args=sys.argv[1],sys.argv[2],json.loads(sys.argv[3])
path="/var/run/qemu-server/%s.qmp"%vmid
err=None
for i in range(80):
    try:
        s=socket.socket(socket.AF_UNIX);s.settimeout(120);s.connect(path);break
    except OSError as e:
        err=e;time.sleep(0.25)
else:
    print(json.dumps({"error":{"desc":"connect: %s"%err}}));sys.exit(0)
f=s.makefile("rwb",buffering=0)
def rd():
    while True:
        l=f.readline()
        if not l: return {"error":{"desc":"closed"}}
        m=json.loads(l)
        if "event" not in m: return m
rd();f.write(b'{"execute":"qmp_capabilities"}\n');rd()
req={"execute":cmd}
if args: req["arguments"]=args
f.write(json.dumps(req).encode()+b"\n");print(json.dumps(rd()))
'''


def pve_qmp(ip, vmid, cmd, args=None, timeout=180):
    o = pve(ip, "python3 -c %s %d %s %s" % (shlex.quote(PVE_QMP_PY), int(vmid), shlex.quote(cmd),
                                             shlex.quote(json.dumps(args or {}))), timeout=timeout)
    r = json.loads(o.strip().splitlines()[-1])
    if "error" in r:
        raise RuntimeError("PVE QMP %s: %s" % (cmd, r["error"].get("desc")))
    return r.get("return")


# ================================================================ Proxmox: SSH
def pve_ssh_cmd(ip, extra=None):
    return ["ssh", "-i", C.ssh_key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "UserKnownHostsFile=" + C.ssh_known, "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=15", "-o", "LogLevel=ERROR"] + (extra or []) + ["root@" + ip]


def pve(ip, script, check=True, timeout=None):
    return run(pve_ssh_cmd(ip) + [script], check=check, timeout=timeout)


def pve_json(ip, path):
    return json.loads(pve(ip, "pvesh get %s --output-format json" % shlex.quote(path)))


def pve_status(ip, node, vmid, kind="qemu"):
    return pve_json(ip, "/nodes/%s/%s/%d/status/current" % (node, kind, vmid)).get("status")


def pve_config(ip, node, vmid, kind="qemu"):
    return pve_json(ip, "/nodes/%s/%s/%d/config" % (node, kind, vmid))


def pve_ha(ip, vmid, kind="qemu"):
    try:
        res = pve_json(ip, "/cluster/ha/resources")
    except Exception:
        return None
    sid = ("vm:%d" if kind == "qemu" else "ct:%d") % vmid
    return next((r for r in res if r.get("sid") == sid), None)


def pve_cmd(kind):
    return "qm" if kind == "qemu" else "pct"


# ================================================================ Proxmox: API (console views)
_api_lock = threading.Lock()
_api_good = [None]
_cache = {}


def pve_api(path, method="GET", params=None, timeout=20):
    """Proxmox API call. With a token: HTTPS to the first host that answers. Without: pvesh over SSH."""
    params = params or {}
    hosts = list(C.pve_hosts)
    if _api_good[0] in hosts:
        hosts.remove(_api_good[0])
        hosts.insert(0, _api_good[0])
    err = "no Proxmox host configured"
    for h in hosts:
        if C.token:
            url = "https://%s:8006/api2/json%s" % (h if ":" not in h else "[%s]" % h, path)
            data = None
            if method == "GET" and params:
                url += "?" + urllib.parse.urlencode(params)
            elif method != "GET":
                data = urllib.parse.urlencode(params).encode()
            req = urllib.request.Request(url, data=data, method=method,
                                         headers={"Authorization": "PVEAPIToken=" + C.token})
            ctx = ssl.create_default_context()
            if not C.verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                    body = r.read()
            except urllib.error.HTTPError as e:
                _api_good[0] = h
                raise RuntimeError("Proxmox API %s: HTTP %d %s" % (path, e.code, e.read()[:300].decode(errors="replace")))
            except (urllib.error.URLError, OSError) as e:
                err = "%s: %s" % (h, getattr(e, "reason", e))
                continue
            _api_good[0] = h
            return json.loads(body).get("data")
        verb = {"GET": "get", "POST": "create", "PUT": "set", "DELETE": "delete"}[method]
        args = "".join(" --%s %s" % (k, shlex.quote(str(v))) for k, v in params.items())
        try:
            o = pve(h, "pvesh %s %s --output-format json%s" % (verb, shlex.quote(path), args), timeout=timeout + 10)
        except RuntimeError as e:
            if "rc 255" in str(e):     # SSH itself failed: try the next host
                err = "%s: %s" % (h, e)
                continue
            raise
        _api_good[0] = h
        o = o.strip()
        return json.loads(o) if o else None
    raise RuntimeError("Proxmox API unreachable (%s)" % err)


def pve_cached(path, ttl, params=None):
    k = path + json.dumps(params or {}, sort_keys=True)
    with _api_lock:
        hit = _cache.get(k)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    v = pve_api(path, "GET", params)
    with _api_lock:
        _cache[k] = (time.time(), v)
    return v


def cache_clear():
    with _api_lock:
        _cache.clear()


def node_ips():
    return {r["name"]: r.get("ip", "") for r in pve_cached("/cluster/status", 60) if r.get("type") == "node"}


# ================================================================ volumes / devices
DISK_RE = re.compile(r"^(scsi|virtio|sata|ide)(\d+)$")
MP_RE = re.compile(r"^mp(\d+)$")


def parse_opts(s):
    parts = s.split(",")
    vol = parts[0]
    opts = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            opts[k] = v
        else:
            opts[p] = "1"
    if "=" in vol and not vol.startswith("/"):
        k, v = vol.split("=", 1)
        opts["_first_key"] = k
        opts["_first_val"] = v
    return vol, opts


def vm_disks(cfg):
    """VM volumes: key, volid, opts, kind (disk|cdrom|cloudinit|efi|tpm)."""
    res = []
    for k, v in cfg.items():
        m = DISK_RE.match(k)
        if m:
            vol, opts = parse_opts(v)
            if vol == "none":
                continue
            kind = "disk"
            if opts.get("media") == "cdrom":
                kind = "cloudinit" if "cloudinit" in vol else "cdrom"
            res.append({"key": k, "volid": vol, "opts": opts, "kind": kind})
        elif k in ("efidisk0", "tpmstate0"):
            vol, opts = parse_opts(v)
            res.append({"key": k, "volid": vol, "opts": opts, "kind": "efi" if k == "efidisk0" else "tpm"})
    res.sort(key=lambda d: d["key"])
    return res


def passthrough(cfg):
    """(dropped, kept) passthrough devices when the VM runs on this host."""
    drop, keep = [], []
    for k, v in sorted(cfg.items()):
        if k.startswith("hostpci"):
            drop.append("%s (%s): PCI device stays in the node" % (k, v))
        elif re.match(r"^usb\d+$", k):
            vol, o = parse_opts(v)
            host = o.get("host") or (o.get("_first_val") if o.get("_first_key") == "host" else None)
            if vol == "spice" or host == "spice":
                keep.append("%s: SPICE USB redirection" % k)
            elif o.get("mapping") or o.get("_first_key") == "mapping":
                drop.append("%s (mapped USB %s): stays in the node" % (k, o.get("mapping") or o.get("_first_val")))
            elif host and re.match(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$", host):
                keep.append("%s: USB %s - attached here whenever that device is plugged in" % (k, host))
            elif host:
                drop.append("%s (USB port %s): port-based USB stays in the node" % (k, host))
        elif re.match(r"^serial\d+$", k) and v.startswith("/dev/"):
            drop.append("%s (%s): host serial port stays in the node" % (k, v))
        elif re.match(r"^parallel\d+$", k):
            drop.append("%s (%s): host parallel port stays in the node" % (k, v))
    return drop, keep


# ================================================================ host status
def cpu_times():
    v = [int(x) for x in readf("/proc/stat").splitlines()[0].split()[1:]]
    return sum(v), v[3] + (v[4] if len(v) > 4 else 0)


def cpu_usage(interval=0.4):
    t1, i1 = cpu_times()
    time.sleep(interval)
    t2, i2 = cpu_times()
    return 0.0 if t2 <= t1 else max(0.0, min(1.0, 1 - (i2 - i1) / (t2 - t1)))


def meminfo():
    m = {}
    for line in readf("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        m[k] = int(v.split()[0]) * 1024
    return m


def fs_usage(path):
    try:
        s = os.statvfs(path)
        return {"total": s.f_blocks * s.f_frsize, "free": s.f_bavail * s.f_frsize, "used": (s.f_blocks - s.f_bfree) * s.f_frsize}
    except OSError:
        return None


def default_iface():
    for line in readf("/proc/net/route").splitlines()[1:]:
        f = line.split()
        if len(f) > 2 and f[1] == "00000000":
            return f[0]
    return ""


def lan_iface():
    return C.lan_iface or default_iface()


def iface_addrs():
    try:
        data = json.loads(run(["ip", "-j", "addr"]))
    except Exception:
        return []
    return [{"name": i.get("ifname"), "state": i.get("operstate"), "mac": i.get("address"),
             "addrs": [a["local"] + "/" + str(a["prefixlen"]) for a in i.get("addr_info", []) if a.get("family") == "inet"]}
            for i in data if i.get("ifname") != "lo"]


def cpuinfo(key):
    for line in readf("/proc/cpuinfo").splitlines():
        if line.startswith(key):
            return line.split(":", 1)[1].strip()
    return ""


def battery():
    for b in ("BAT0", "BAT1"):
        p = "/sys/class/power_supply/" + b
        if os.path.isdir(p):
            return {"capacity": readf(p + "/capacity").strip(), "status": readf(p + "/status").strip()}
    return None


def host_gateway():
    """The LAN gateway: from the config, else the default route."""
    if C.gateway:
        return C.gateway
    for line in readf("/proc/net/route").splitlines()[1:]:
        f = line.split()
        if len(f) > 2 and f[1] == "00000000":
            return socket.inet_ntoa(struct.pack("<I", int(f[2], 16)))
    return ""


def cmd_host_status(args=None):
    mi = meminfo()
    return {
        "ok": True, "version": VERSION, "hostname": socket.gethostname(), "name": C.host_name, "tag": my_tag(),
        "time": int(time.time()), "uptime": float(readf("/proc/uptime", "0 0").split()[0]),
        "loadavg": [float(x) for x in readf("/proc/loadavg").split()[:3]],
        "cpu": cpu_usage(), "cpus": os.cpu_count(), "cpu_model": cpuinfo("model name"), "cpu_vendor": cpuinfo("vendor_id"),
        "kvm": os.path.exists("/dev/kvm"),
        "mem": {"total": mi.get("MemTotal", 0), "free": mi.get("MemAvailable", 0), "used": mi.get("MemTotal", 0) - mi.get("MemAvailable", 0)},
        "swap": {"total": mi.get("SwapTotal", 0), "free": mi.get("SwapFree", 0), "used": mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)},
        "rootfs": fs_usage("/"), "datafs": fs_usage(C.data), "data_dir": C.data, "kernel": os.uname().release,
        "os": dict(re.findall(r'^(\w+)="?([^"\n]*)"?', readf("/etc/os-release"), re.M)).get("PRETTY_NAME", ""),
        "qemu": (run([C.kvm, "--version"], check=False).splitlines() or [""])[0] if os.path.exists(C.kvm) else "",
        "lxc": run(["lxc-start", "--version"], check=False).strip() if shutil.which("lxc-start") else "",
        "ifaces": iface_addrs(), "default_iface": default_iface(), "lan_iface": lan_iface(),
        "bridge": C.bridge, "nat_bridge": C.nat_bridge, "nat_network": str(C.nat_net.network),
        "tailscale": run(["tailscale", "ip", "-4"], check=False).strip() if shutil.which("tailscale") else "",
        "battery": battery(), "guests": list_guests(),
    }


# ================================================================ guests
ACTIVE = ("on-host", "returning", "importing")


def all_metas():
    res = []
    if os.path.isdir(C.vms):
        for d in sorted(os.listdir(C.vms), key=lambda x: int(x) if x.isdigit() else 0):
            m = load_json(C.vms + "/" + d + "/meta.json") if d.isdigit() else None
            if m:
                res.append(m)
    return res


def managed():
    return [m for m in all_metas() if m.get("state") in ACTIVE]


def list_guests():
    return [guest_summary(m) for m in all_metas()]


def guest_status(m):
    if m.get("state") not in ACTIVE:
        return m.get("state")
    return ct_state(m) if m["kind"] == "lxc" else vm_state(m)


def guest_summary(m):
    return {"vmid": m["vmid"], "name": m["name"], "kind": m["kind"],
            "pp_state": m.get("state"), "status": guest_status(m), "origin_node": m.get("origin_node"),
            "cores": m.get("vcpus"), "memory": m.get("memory_mb"), "sync_interval": m.get("sync_interval"),
            "last_sync": (m.get("syncs") or [None])[-1], "net_mode": m.get("net_mode"),
            "guest_ip": m.get("guest_ip"), "moved": m.get("move_type")}


def cmd_list(args=None):
    return {"ok": True, "guests": list_guests()}


def cmd_vm_status(args):
    m = load_meta(int(args[0]))
    st = {"ok": True, "meta": m, "status": guest_status(m)}
    if m["kind"] == "lxc":
        st.update(ct_status(m))
    else:
        st["chain"] = chain_info(m)
        if st["status"] in ("running", "paused"):
            st["stats"] = vm_stats(m)
            st["ips"] = vm_ips(m)
    return st


def chain_info(m):
    res = {}
    d = vmdir(m["vmid"])
    for dk in m["disks"]:
        if dk["kind"] != "disk":
            continue
        res[dk["key"]] = [{"file": f, "alloc": (os.stat(d + "/" + f).st_blocks * 512) if os.path.exists(d + "/" + f) else None}
                          for f in [dk["base"]] + dk.get("overlays", [])]
    return res


def power(action, args):
    m = load_meta(int(args[0]))
    vmid = m["vmid"]
    if m.get("state") != "on-host":
        raise UserError("guest %d is in state %s, not on this host" % (vmid, m.get("state")))
    if m["kind"] == "lxc":
        ct_power(m, action)
    elif action == "start":
        guard_pve(m)
        vm_start(m)
        if m.get("guest_ip") and m["net_mode"] == "lan":
            garp(m["guest_ip"])
    elif action == "shutdown":
        qmp(qmp_path(vmid), "system_powerdown")
    elif action == "stop":
        vm_kill(m)
    elif action == "reboot":
        try:
            qga(qga_path(vmid), "guest-shutdown", {"mode": "reboot"}, timeout=10)
        except Exception:
            qmp(qmp_path(vmid), "system_reset")
    time.sleep(0.5)
    return {"ok": True, "status": guest_status(m)}


def guard_pve(m, offline_ok=True):
    """Refuse to run/sync the local copy if the Proxmox copy runs (split brain) or was taken
    back by hand (its tag for this host is gone)."""
    try:
        s = pve_status(m["origin_ip"], m["origin_node"], m["vmid"], m["kind"])
        cfg = pve_config(m["origin_ip"], m["origin_node"], m["vmid"], m["kind"])
    except Exception as e:
        if offline_ok:
            return "unreachable (%s)" % e
        raise RuntimeError("cannot reach %s to check guest %d: %s" % (m["origin_node"], m["vmid"], e))
    if s == "running":
        raise RuntimeError("the Proxmox copy of %d is RUNNING on %s - refusing" % (m["vmid"], m["origin_node"]))
    if my_tag() not in tag_list(cfg.get("tags", "")):
        raise RuntimeError("guest %d on %s no longer carries tag %s - it was taken back by hand; refusing"
                           % (m["vmid"], m["origin_node"], my_tag()))
    return s


# ================================================================ networking
def _wr(path, val):
    try:
        with open(path, "w") as fh:
            fh.write(val)
    except OSError:
        pass


def _fwd_accept(br):
    if shutil.which("iptables"):   # Docker and others can set the FORWARD policy to DROP
        for rule in (["-i", br, "-j", "ACCEPT"], ["-o", br, "-j", "ACCEPT"]):
            if subprocess.run(["iptables", "-C", "FORWARD"] + rule, capture_output=True).returncode != 0:
                run(["iptables", "-I", "FORWARD", "1"] + rule, check=False)


def _bridge(name):
    if not os.path.exists("/sys/class/net/" + name):
        run(["ip", "link", "add", name, "type", "bridge"])
    run(["ip", "link", "set", name, "up"])


def net_up():
    """Both guest bridges (idempotent): the routed LAN bridge and the NAT bridge."""
    os.makedirs(C.run, exist_ok=True)
    lan_up()
    nat_up()


def lan_up():
    """Routed bridge for LAN-mode guests: /32 routes, proxy ARP, static DHCP leases."""
    br = C.bridge
    _bridge(br)
    if "169.254.77.1" not in run(["ip", "-4", "addr", "show", "dev", br], check=False):
        run(["ip", "addr", "add", "169.254.77.1/16", "dev", br], check=False)
    lan = lan_iface()
    gw = host_gateway()
    mine = []
    try:
        for i in json.loads(run(["ip", "-j", "-4", "addr", "show", "dev", lan])) if lan else []:
            mine += [a["local"] for a in i.get("addr_info", []) if a.get("family") == "inet"]
    except Exception:
        pass
    lan_ip = next((x for x in mine if gw and x.startswith(gw.rsplit(".", 1)[0] + ".")), mine[0] if mine else None)
    for i in json.loads(run(["ip", "-j", "-4", "addr", "show", "dev", br])):
        for a_ in i.get("addr_info", []):
            if a_.get("prefixlen") == 32 and a_["local"] != lan_ip:
                run(["ip", "addr", "del", a_["local"] + "/32", "dev", br], check=False)
    if lan_ip:
        # dnsmasq needs an address in the guests' subnet to lease from; a /32 doesn't change routing
        run(["ip", "addr", "replace", lan_ip + "/32", "dev", br], check=False)
    _wr("/proc/sys/net/ipv4/ip_forward", "1")
    _wr("/proc/sys/net/ipv4/conf/%s/proxy_arp" % br, "1")
    if lan:
        _wr("/proc/sys/net/ipv4/conf/%s/proxy_arp" % lan, "1")
    _fwd_accept(br)
    # A guest that arrives live still has the real router's MAC cached for its gateway. Rewrite every
    # unicast IP frame entering the bridge from a guest port to the bridge's own MAC, so this host
    # routes it whatever MAC the guest used.
    if shutil.which("nft"):
        brmac = readf("/sys/class/net/%s/address" % br).strip()
        rules = ("table bridge proxmox_portable {\n chain pre {\n  type filter hook prerouting priority -300; policy accept;\n"
                 "  ibrname \"%s\" ether daddr & 01:00:00:00:00:00 == 00:00:00:00:00:00 ether daddr != %s "
                 "ether type { ip, ip6 } ether daddr set %s meta pkttype set host\n }\n}\n" % (br, brmac, brmac))
        run(["nft", "delete", "table", "bridge", "proxmox_portable"], check=False)
        run(["nft", "-f", "-"], input=rules, check=False)
    for m in managed():
        if m.get("net_mode") == "lan" and m.get("guest_ip"):
            run(["ip", "route", "replace", m["guest_ip"] + "/32", "dev", br], check=False)
    dnsmasq_lan()


def dnsmasq_lan():
    # under /run, not the 0700 config dir: dnsmasq drops to an unprivileged user before reading it
    os.makedirs(C.run, exist_ok=True)
    os.chmod(C.run, 0o755)
    hosts = C.run + "/dnsmasq-hosts"
    pidf = C.run + "/dnsmasq-lan.pid"
    lines = []
    for m in managed():
        if m.get("net_mode") == "lan" and m.get("guest_ip"):
            for mac in m.get("lan_macs", []):
                lines.append("%s,%s,%s" % (mac.lower(), m["guest_ip"], re.sub(r"[^A-Za-z0-9-]", "-", m["name"])))
    with open(hosts, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    os.chmod(hosts, 0o644)
    pid = readf(pidf).strip()
    if pid.isdigit() and os.path.exists("/proc/" + pid):
        if lines and hosts in readf("/proc/%s/cmdline" % pid):
            os.kill(int(pid), 1)          # SIGHUP: re-read the hosts file
            return
        os.kill(int(pid), 15)             # nothing to serve, or started with other settings
        time.sleep(0.3)
    gw = host_gateway()
    if not lines or not gw:
        return
    net = str(ipaddress.ip_interface("%s/%d" % (gw, C.prefix)).network.network_address)
    dns = C.dns or [gw]
    run(["dnsmasq", "--conf-file=/dev/null", "--port=0", "--interface=" + C.bridge, "--bind-dynamic",
         "--except-interface=lo", "--pid-file=" + pidf,
         "--dhcp-range=%s,static,%s,12h" % (net, prefix_to_mask(C.prefix)), "--dhcp-hostsfile=" + hosts,
         "--dhcp-option=option:router," + gw, "--dhcp-option=option:dns-server," + ",".join(dns),
         "--dhcp-authoritative", "--dhcp-leasefile=%s/dnsmasq-lan.leases" % C.run, "--log-dhcp",
         "--log-facility=/var/log/proxmox-portable-dnsmasq.log"], check=False)


def nat_up():
    """NAT bridge with DHCP and DNS for NAT-mode guests (replaces libvirt's default network)."""
    br, ni = C.nat_bridge, C.nat_net
    _bridge(br)
    if str(ni) not in run(["ip", "-4", "addr", "show", "dev", br], check=False):
        run(["ip", "addr", "add", str(ni), "dev", br], check=False)
    _wr("/proc/sys/net/ipv4/ip_forward", "1")
    _fwd_accept(br)
    if shutil.which("nft"):
        rules = ("table ip proxmox_portable_nat {\n chain post {\n  type nat hook postrouting priority 100; policy accept;\n"
                 "  ip saddr %s oifname != \"%s\" masquerade\n }\n}\n" % (ni.network, br))
        run(["nft", "delete", "table", "ip", "proxmox_portable_nat"], check=False)
        run(["nft", "-f", "-"], input=rules, check=False)
    pidf = C.run + "/dnsmasq-nat.pid"
    pid = readf(pidf).strip()
    if pid.isdigit() and os.path.exists("/proc/" + pid):
        return
    hosts = list(ni.network.hosts())
    lo, hi = hosts[min(99, len(hosts) // 2)], hosts[-1]
    run(["dnsmasq", "--conf-file=/dev/null", "--interface=" + br, "--bind-dynamic", "--except-interface=lo",
         "--pid-file=" + pidf, "--dhcp-range=%s,%s,%s,12h" % (lo, hi, ni.network.netmask),
         "--dhcp-option=option:router,%s" % ni.ip, "--dhcp-option=option:dns-server,%s" % ni.ip,
         "--dhcp-leasefile=%s/dnsmasq-nat.leases" % C.run, "--dhcp-authoritative"], check=False)


def prefix_to_mask(p):
    v = (0xFFFFFFFF << (32 - int(p))) & 0xFFFFFFFF
    return ".".join(str((v >> s) & 255) for s in (24, 16, 8, 0))


def net_guest_up(m):
    net_up()


def net_guest_down(m):
    if m.get("net_mode") == "lan" and m.get("guest_ip"):
        run(["ip", "route", "del", m["guest_ip"] + "/32", "dev", C.bridge], check=False)


def garp(ip, count=3):
    """Gratuitous ARP on the LAN: `ip` now lives behind this host's MAC."""
    lan = lan_iface()
    if not lan or not ip:
        return
    try:
        mac = bytes.fromhex(readf("/sys/class/net/%s/address" % lan).strip().replace(":", ""))
        ipb = socket.inet_aton(ip)
        frame = (b"\xff" * 6 + mac + b"\x08\x06" + struct.pack("!HHBBH", 1, 0x0800, 6, 4, 2)
                 + mac + ipb + b"\xff" * 6 + ipb)
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        s.bind((lan, 0))
        for _ in range(count):
            s.send(frame)
            time.sleep(0.2)
        s.close()
    except OSError:
        pass


def cmd_ifup(args):
    """QEMU tap script: put tap<vmid>i<n> on the bridge chosen for that NIC."""
    tap = args[0]
    mm = re.match(r"^tap(\d+)i(\d+)$", tap)
    br = C.nat_bridge
    if mm:
        m = load_json(vmdir(int(mm.group(1))) + "/meta.json") or {}
        br = (m.get("bridges") or {}).get("net" + mm.group(2), C.nat_bridge)
    run(["ip", "link", "set", tap, "up"])
    run(["ip", "link", "set", tap, "master", br])
    sys.exit(0)


def cmd_ifdown(args):
    run(["ip", "link", "set", args[0], "down"], check=False)
    sys.exit(0)


def write_helpers():
    """Tap up/down scripts QEMU calls (they call back into this script)."""
    os.makedirs(C.helpers, exist_ok=True)
    for name, sub in (("ifup", "ifup"), ("ifdown", "ifdown")):
        p = "%s/%s" % (C.helpers, name)
        body = "#!/bin/sh\nexec %s %s --config %s %s \"$1\"\n" % (
            shlex.quote(sys.executable), shlex.quote(SELF), shlex.quote(C.path), sub)
        if readf(p) != body:
            with open(p, "w") as f:
                f.write(body)
        os.chmod(p, 0o755)


# ================================================================ Proxmox-QEMU runtime
def qmp_path(vmid):
    return "%s/%d.qmp" % (QRUN, vmid)


def qga_path(vmid):
    return "%s/%d.qga" % (QRUN, vmid)


def pid_path(vmid):
    return "%s/%d.pid" % (QRUN, vmid)


def vm_pid(vmid):
    pid = readf(pid_path(vmid)).strip()
    if pid.isdigit() and os.path.exists("/proc/" + pid):
        if "-id\x00%d\x00" % vmid in readf("/proc/%s/cmdline" % pid):
            return int(pid)
    return None


def vm_state(m):
    if not vm_pid(m["vmid"]):
        return "stopped"
    try:
        s = qmp(qmp_path(m["vmid"]), "query-status", timeout=5).get("status")
        return {"inmigrate": "migrating", "postmigrate": "migrated", "prelaunch": "paused"}.get(s, s)
    except Exception:
        return "running"


def leaf_rewrite(node, newfile, fmt=None):
    """Point the protocol leaf of a Proxmox -blockdev tree at a local file (and set its format)."""
    chain = [node]
    while isinstance(chain[-1].get("file"), dict):
        chain.append(chain[-1]["file"])
    leaf = chain[-1]
    leaf["driver"] = "file"
    leaf["filename"] = newfile
    if fmt and len(chain) >= 2:
        chain[-2]["driver"] = fmt
    return node


def build_argv(m, incoming=False):
    """Rewrite the Proxmox command line for this host. Only disk paths, tap scripts and passthrough
    devices change; machine type, firmware, MACs, UUID and vmgenid stay exactly as Proxmox set them."""
    d = vmdir(m["vmid"])
    local = {}   # node/drive id -> (path, format)
    for dk in m["disks"]:
        if dk["kind"] == "disk":
            img = dk["overlays"][-1] if dk.get("overlays") else dk["base"]
            local["drive-" + dk["key"]] = (d + "/" + img, "qcow2" if img.endswith(".qcow2") else "raw")
        elif dk["kind"] == "cloudinit" and dk.get("base"):
            local["drive-" + dk["key"]] = (d + "/" + dk["base"], "raw")
        elif dk["kind"] == "efi":
            local["drive-efidisk0"] = (d + "/" + dk["base"], "raw")
    a = list(m["pve_argv"])
    res = [C.kvm]
    dropped = set()
    i = 1
    while i < len(a):
        x = a[i]
        if x in FLAG_OPTS:
            if x != "-no-shutdown":       # without qmeventd, QEMU must exit on guest poweroff
                res.append(x)
            i += 1
            continue
        y = a[i + 1] if i + 1 < len(a) else ""
        i += 2
        if x == "-chardev" and "id=qmp-event" in y:
            continue
        if x == "-object" and "chardev=qmp-event" in y:
            continue
        if x == "-blockdev":
            bd = json.loads(y)
            nn = bd.get("node-name", "")
            if nn in local:
                y = json.dumps(leaf_rewrite(bd, *local[nn]))
            elif nn.startswith("drive-"):     # a guest volume we did not copy (ISO CD-ROM)
                dropped.add(nn)
                continue
            # anything else (PVE 9 firmware pflash0, ...) points at files the bundle provides: keep
        elif x == "-drive" and "if=pflash" in y and "unit=1" in y and "drive-efidisk0" in local:
            y = re.sub(r"file=[^,]+", "file=" + local["drive-efidisk0"][0], y)
        elif x == "-netdev" and "type=tap" in y:
            y = re.sub(r"(?<![a-z])script=[^,]+", "script=%s/ifup" % C.helpers, y)
            y = re.sub(r"downscript=[^,]+", "downscript=%s/ifdown" % C.helpers, y)
        elif x == "-device":
            if y.startswith("vfio-pci"):
                continue                   # PCI passthrough stays in the node
            if y.startswith("usb-host") and ("hostbus=" in y or "hostport=" in y):
                continue                   # port-based USB stays in the node
            dm = re.search(r"drive=([^,]+)", y)
            if dm and dm.group(1) in dropped:
                if y.startswith(("ide-cd", "scsi-cd")):
                    y = re.sub(r",?drive=[^,]+", "", y)    # empty CD drive
                else:
                    continue
        elif x == "-machine" and m.get("machine"):
            y = re.sub(r"type=[^,]+", "type=" + m["machine"], y)
        res += [x, y]
    if incoming:
        res += ["-incoming", "defer", "-S"]
    return res


def start_swtpm(m, statefile):
    vmid = m["vmid"]
    sock, pidf = "%s/%d.swtpm" % (QRUN, vmid), "%s/%d.swtpm.pid" % (QRUN, vmid)
    old = readf(pidf).strip()
    if old.isdigit() and os.path.exists("/proc/" + old):
        os.kill(int(old), 15)
        time.sleep(0.5)
    for f in (sock, pidf):
        if os.path.exists(f):
            os.unlink(f)
    tpm = next(dk for dk in m["disks"] if dk["kind"] == "tpm")
    cmd = [C.swtpm, "socket", "--tpmstate", "backend-uri=file://%s,mode=0600" % statefile,
           "--ctrl", "type=unixio,path=%s,mode=0600" % sock, "--pid", "file=" + pidf, "--terminate", "--daemon",
           "--log", "file=%s/%d-swtpm.log,level=1" % (QRUN, vmid)]
    if tpm["opts"].get("version", "v2.0") == "v2.0":
        cmd.append("--tpm2")
    run(cmd)
    for _ in range(100):
        if os.path.exists(pidf):
            return
        time.sleep(0.05)
    raise RuntimeError("swtpm did not start")


def vm_start(m, incoming=False):
    vmid = m["vmid"]
    if vm_pid(vmid):
        raise RuntimeError("VM %d is already running here" % vmid)
    if not os.path.exists(C.kvm):
        raise RuntimeError("Proxmox QEMU bundle missing at %s; run `proxmox-portable fetch-qemu`" % C.pveq)
    os.makedirs(QRUN, exist_ok=True)
    os.makedirs(C.run, exist_ok=True)
    write_helpers()
    net_guest_up(m)
    tpm = next((dk for dk in m["disks"] if dk["kind"] == "tpm"), None)
    if tpm:
        start_swtpm(m, vmdir(vmid) + "/" + tpm["base"])
    argv = build_argv(m, incoming)
    with open(vmdir(vmid) + "/cmdline", "w") as f:
        f.write(" ".join(shlex.quote(x) for x in argv) + "\n")
    unit = "pportable-vm-%d" % vmid
    run(["systemctl", "reset-failed", unit], check=False)
    run(["systemd-run", "--unit", unit, "--collect", "--service-type=forking", "--property=PIDFile=" + pid_path(vmid),
         "--property=KillMode=process", "--property=TimeoutStopSec=30",
         "--description=proxmox-portable VM %d (%s)" % (vmid, m["name"])] + argv)
    for _ in range(100):
        if vm_pid(vmid):
            break
        time.sleep(0.1)
    if not vm_pid(vmid):
        raise RuntimeError("QEMU did not start; see journalctl -u %s" % unit)
    if not incoming:
        record_machine(m)


def record_machine(m):
    try:
        for mm in qmp(qmp_path(m["vmid"]), "query-machines"):
            if mm.get("is-current"):
                cur = mm["name"] + ("+" + mm["pve-version"] if mm.get("pve-version") else "")
                if m.get("machine") != cur:
                    m["machine"] = cur
                    save_meta(m)
                return cur
    except Exception:
        pass


def vm_kill(m):
    vmid = m["vmid"]
    try:
        qmp(qmp_path(vmid), "quit", timeout=10)
    except Exception:
        pass
    for _ in range(50):
        if not vm_pid(vmid):
            break
        time.sleep(0.2)
    pid = vm_pid(vmid)
    if pid:
        os.kill(pid, 9)
    sp = readf("%s/%d.swtpm.pid" % (QRUN, vmid)).strip()
    if sp.isdigit() and os.path.exists("/proc/" + sp):
        os.kill(int(sp), 15)


def vm_shutdown_wait(m, tmo, force, log):
    vmid = m["vmid"]
    if not vm_pid(vmid):
        return
    log("shutting VM %d down on this host (timeout %ss)" % (vmid, tmo))
    try:
        qmp(qmp_path(vmid), "system_powerdown")
    except Exception as e:
        log("powerdown: %s" % e)
    t = time.time() + tmo
    while time.time() < t and vm_pid(vmid):
        time.sleep(2)
    if vm_pid(vmid):
        if not force:
            raise RuntimeError("guest did not shut down within %ss; not forcing it" % tmo)
        log("clean shutdown failed, forcing off (force_stop was chosen)")
        vm_kill(m)


def vm_stats(m):
    vmid = m["vmid"]
    res = {"rd": 0, "wr": 0}
    try:
        for b in qmp(qmp_path(vmid), "query-blockstats", timeout=5):
            res["rd"] += b.get("stats", {}).get("rd_bytes", 0)
            res["wr"] += b.get("stats", {}).get("wr_bytes", 0)
    except Exception:
        pass
    try:
        bal = qmp(qmp_path(vmid), "query-balloon", timeout=5)
        res["mem_actual"] = bal.get("actual")
        if bal.get("free_mem") is not None and bal.get("total_mem"):
            res["mem_used"] = bal["total_mem"] - bal["free_mem"]
    except Exception:
        pass
    rx = tx = 0
    for n in os.listdir("/sys/class/net"):
        if re.match(r"^tap%di\d+$" % vmid, n):   # host rx = guest tx
            tx += int(readf("/sys/class/net/%s/statistics/rx_bytes" % n, "0"))
            rx += int(readf("/sys/class/net/%s/statistics/tx_bytes" % n, "0"))
    res["rx"], res["tx"] = rx, tx
    pid = vm_pid(vmid)
    if pid:
        f = readf("/proc/%d/stat" % pid).rsplit(")", 1)[1].split()
        res["cpu_ticks"] = int(f[11]) + int(f[12])
        res["rss"] = int(f[21]) * os.sysconf("SC_PAGE_SIZE")
    return res


def vm_ips(m):
    try:
        ips = [a["ip-address"] for i in qga(qga_path(m["vmid"]), "guest-network-get-interfaces", timeout=3)
               for a in i.get("ip-addresses", []) if a.get("ip-address-type") == "ipv4" and not a["ip-address"].startswith("127.")]
        return {"source": "agent", "ips": ips}
    except Exception:
        return {"source": None, "ips": [m["guest_ip"]] if m.get("guest_ip") else []}


def node_by_file(vmid, path):
    for n in qmp(qmp_path(vmid), "query-named-block-nodes", {"flat": True}):
        if n.get("file") == path and n.get("drv") in ("qcow2", "raw"):
            return n["node-name"]
    raise RuntimeError("no block node for %s" % path)


def wait_job(vmid, job_id, tmo=86400):
    t = time.time() + tmo
    while time.time() < t:
        j = {x["id"]: x for x in qmp(qmp_path(vmid), "query-jobs")}.get(job_id)
        if not j:
            return None
        if j.get("status") == "concluded":
            qmp(qmp_path(vmid), "job-dismiss", {"id": job_id})
            if j.get("error"):
                raise RuntimeError("job %s: %s" % (job_id, j["error"]))
            return j
        time.sleep(0.5)
    raise RuntimeError("job %s timed out" % job_id)


# ================================================================ jobs
def new_job(kind, vmid, params, timer=False):
    os.makedirs(C.jobs, exist_ok=True)
    jid = time.strftime("%Y%m%d-%H%M%S-") + kind + "-" + str(vmid)
    write_json(C.jobs + "/" + jid + ".json", {"id": jid, "kind": kind, "vmid": vmid, "params": params,
                                              "status": "queued", "created": int(time.time()), "timer": timer})
    # a transient systemd unit, so a job survives a restart of the web console
    run(["systemd-run", "--unit", "pportable-job-" + re.sub(r"[^A-Za-z0-9-]", "-", jid), "--collect",
         "--property=KillMode=process", "--property=TimeoutStopSec=60",
         sys.executable, SELF, "--config", C.path, "_run-job", jid])
    return jid


def job_set(jid, **kw):
    p = C.jobs + "/" + jid + ".json"
    j = load_json(p, {})
    j.update(kw)
    write_json(p, j)


def job_progress(jid, **kw):
    if jid:
        job_set(jid, progress=kw)


def cmd_run_job(args):
    jid = args[0]
    j = load_json(C.jobs + "/" + jid + ".json")
    log = Log(C.jobs + "/" + jid + ".log")
    job_set(jid, status="running", started=int(time.time()), pid=os.getpid())
    try:
        kind = j["params"].get("kind") or (load_json(vmdir(j["vmid"]) + "/meta.json") or {}).get("kind", "qemu")
        fn = {("import", "qemu"): do_import, ("sync", "qemu"): do_sync, ("return", "qemu"): do_return,
              ("import", "lxc"): ct_import, ("sync", "lxc"): do_sync, ("return", "lxc"): ct_return}[(j["kind"], kind)]
        res = fn(j["params"], log, jid)
        job_set(jid, status="ok", ended=int(time.time()), result=res)
        log("JOB OK")
    except Exception as e:
        log("JOB FAILED: %s" % e)
        job_set(jid, status="failed", ended=int(time.time()), error=str(e))
        sys.exit(1)
    sys.exit(0)


def job_running(vmid):
    for j in cmd_jobs()["jobs"]:
        if j.get("vmid") == vmid and j.get("status") in ("queued", "running"):
            return j["id"]
    return None


def cmd_jobs(args=None):
    res = []
    if os.path.isdir(C.jobs):
        for f in sorted(os.listdir(C.jobs), reverse=True):
            if f.endswith(".json"):
                j = load_json(C.jobs + "/" + f)
                if j:
                    if j.get("status") == "running" and j.get("pid") and not os.path.exists("/proc/%d" % j["pid"]):
                        j["status"], j["error"] = "failed", "job process vanished"
                    if j.get("status") == "queued" and time.time() - j.get("created", 0) > 120:
                        j["status"], j["error"] = "failed", "job never started"
                    res.append(j)
            if len(res) >= 80:
                break
    return {"ok": True, "jobs": res}


def cmd_job(args):
    jid = re.sub(r"[^A-Za-z0-9._-]", "", args[0])
    j = load_json(C.jobs + "/" + jid + ".json")
    if not j:
        raise UserError("no such job")
    j["log"] = readf(C.jobs + "/" + jid + ".log")[-20000:]
    return {"ok": True, "job": j}


def vm_lock(vmid):
    os.makedirs(vmdir(vmid), exist_ok=True)
    fh = open(vmdir(vmid) + "/.lock", "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


# ================================================================ shared import/return pieces
def stream_volume(m, dk, log, jid, label):
    """Copy one Proxmox volume to a sparse local raw file, sha256-verified on both ends."""
    ip = m["origin_ip"]
    path, size = dk["pve_path"], dk["size"]
    dst = vmdir(m["vmid"]) + "/" + dk["base"]
    log("copying %s (%s, %.2f GiB) from %s:%s" % (dk["key"], dk["volid"], size / 2**30, m["origin_node"], path))
    remote = ("set -o pipefail; f=$(mktemp); dd if=%s bs=4M status=none | tee >(sha256sum | cut -c1-64 > $f) "
              "| zstd -1 -T2 -q; rc=$?; sleep 1; echo \"PPSHA $(cat $f)\" >&2; rm -f $f; exit $rc") % shlex.quote(path)
    ssh = subprocess.Popen(pve_ssh_cmd(ip) + ["bash -c " + shlex.quote(remote)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    unz = subprocess.Popen(["zstd", "-d", "-q", "-c"], stdin=ssh.stdout, stdout=subprocess.PIPE)
    ssh.stdout.close()
    h = hashlib.sha256()
    CH = 4 << 20
    zero = bytes(CH)
    done, t0, last = 0, time.time(), 0
    with open(dst, "wb") as f:
        f.truncate(size)
        while True:
            buf = unz.stdout.read(CH)
            if not buf:
                break
            h.update(buf)
            if buf == zero[:len(buf)]:
                f.seek(len(buf), 1)
            else:
                f.write(buf)
            done += len(buf)
            if time.time() - last > 3:
                last = time.time()
                job_progress(jid, step=label, done=done, total=size, rate=done / max(1, time.time() - t0))
        f.flush()
        os.fsync(f.fileno())
    unz.wait()
    err = ssh.stderr.read().decode(errors="replace")
    ssh.wait()
    if ssh.returncode or unz.returncode:
        raise RuntimeError("copy of %s failed (ssh rc %s, zstd rc %s): %s" % (dk["key"], ssh.returncode, unz.returncode, err[-500:]))
    rsha = (re.findall(r"PPSHA ([0-9a-f]{64})", err) or [""])[0]
    if done != size:
        raise RuntimeError("copy of %s: got %d bytes, expected %d" % (dk["key"], done, size))
    if rsha != h.hexdigest():
        raise RuntimeError("checksum mismatch on %s" % dk["key"])
    log("  %s copied, %.1f MiB/s, sha256 %s verified" % (dk["key"], size / 2**20 / max(1, time.time() - t0), rsha[:16]))


def volume_info(ip, volid):
    path = pve(ip, "pvesm path %s" % shlex.quote(volid)).strip()
    fmt = "raw" if path.startswith("/dev/") else json.loads(
        pve(ip, "qemu-img info --output=json %s" % shlex.quote(path))).get("format", "raw")
    size = int(pve(ip, "blockdev --getsize64 %s 2>/dev/null || stat -c %%s %s" % (shlex.quote(path), shlex.quote(path))).strip())
    used = size
    if path.startswith("/dev/zvol/"):
        try:
            used = int(pve(ip, "zfs get -Hp -o value logicalreferenced %s" % shlex.quote(path[10:])).strip())
        except Exception:
            pass
    return path, fmt, size, used


def common_meta(p, cfg, kind):
    vmid = int(p["vmid"])
    m = {"vmid": vmid, "kind": kind,
         "name": cfg.get("name") or cfg.get("hostname") or "guest%d" % vmid,
         "origin_node": p["node"], "origin_ip": p["node_ip"], "state": "importing",
         "pve_config": cfg, "pve_onboot": cfg.get("onboot"), "pve_tags": cfg.get("tags", ""),
         "vcpus": int(cfg.get("sockets", 1)) * int(cfg.get("cores", 1)), "memory_mb": int(str(cfg.get("memory", 512))),
         "net_mode": p.get("net_mode", "lan"), "guest_ip": p.get("guest_ip") or None, "lan_macs": [], "bridges": {},
         "sync_interval": p.get("sync_interval", C.sync_default), "syncs": [], "created": int(time.time()),
         "host": C.host_name, "disks": [], "move_type": p.get("move", "cold")}
    for k in cfg:
        if re.match(r"^net\d+$", k):
            vol, o = parse_opts(cfg[k])
            mac = o.get("_first_val") if kind == "qemu" else o.get("hwaddr")
            on_lan = o.get("bridge") in C.lan_bridges and m["net_mode"] == "lan"
            m["bridges"][k] = C.bridge if on_lan else C.nat_bridge
            if on_lan and mac:
                m["lan_macs"].append(mac)
    if m["net_mode"] == "lan" and not m["guest_ip"]:
        raise RuntimeError("network mode 'lan' needs the guest's LAN IP")
    return m


def park_pve(m, log):
    tags = [t for t in tag_list(m["pve_tags"]) if not t.startswith(C.tag_prefix)] + [my_tag()]
    pve(m["origin_ip"], "%s set %d --onboot 0 --tags %s" % (pve_cmd(m["kind"]), m["vmid"], shlex.quote(";".join(tags))))
    log("parked %d on %s: onboot=0, tag %s" % (m["vmid"], m["origin_node"], my_tag()))


def restore_pve(m, log, start=False, already_running=False):
    ip, node, vmid, kind = m["origin_ip"], m["origin_node"], m["vmid"], m["kind"]
    tags = [t for t in tag_list(m.get("pve_tags")) if not t.startswith(C.tag_prefix)]
    cmd = pve_cmd(kind)
    args = "--onboot %s" % (m.get("pve_onboot") or 0)
    if tags:
        args += " --tags %s" % shlex.quote(";".join(tags))
    pve(ip, "%s set %d %s" % (cmd, vmid, args))
    if not tags:
        pve(ip, "%s set %d --delete tags" % (cmd, vmid), check=False)
    pve(ip, "%s unlock %d" % (cmd, vmid), check=False)
    ha = m.get("pve_ha")
    if ha:
        opts = "".join(" --%s %s" % (k, shlex.quote(str(ha[k]))) for k in ("group", "max_relocate", "max_restart", "comment")
                       if ha.get(k) not in (None, ""))
        st = ha.get("state", "started")
        if not (start or already_running) and st == "started":
            st = "stopped"
        pve(ip, "ha-manager add %s --state %s%s" % (ha["sid"], st, opts))
        log("re-added %s to HA (state %s)" % (ha["sid"], st))
    elif start and not already_running:
        pve(ip, "%s start %d" % (cmd, vmid))
        log("started %d on %s" % (vmid, node))
    log("Proxmox %d restored: onboot=%s, tags=%s" % (vmid, m.get("pve_onboot") or 0, ";".join(tags)))


def ha_release(m, status, log, stop=True, tmo=300):
    """Take the guest out of HA so HA can't restart the parked copy."""
    ha = m.get("pve_ha")
    if not ha:
        return False
    log("removing %s from HA (was state %s); it is restored on return" % (ha["sid"], ha.get("state")))
    if stop and status == "running":
        pve(m["origin_ip"], "ha-manager set %s --state stopped" % ha["sid"])
        wait_pve_stopped(m, tmo)
    pve(m["origin_ip"], "ha-manager remove %s" % ha["sid"])
    return True


def wait_pve_stopped(m, timeout):
    t = time.time() + timeout
    while time.time() < t:
        if pve_status(m["origin_ip"], m["origin_node"], m["vmid"], m["kind"]) != "running":
            return
        time.sleep(5)
    raise RuntimeError("%d still running on %s after %ss" % (m["vmid"], m["origin_node"], timeout))


def pve_shutdown(m, p, log):
    ip, node, vmid, cmd = m["origin_ip"], m["origin_node"], m["vmid"], pve_cmd(m["kind"])
    if pve_status(ip, node, vmid, m["kind"]) != "running":
        return
    tmo = int(p.get("shutdown_timeout", 300))
    log("shutting %d down on %s (timeout %ss)" % (vmid, node, tmo))
    run(pve_ssh_cmd(ip) + ["%s shutdown %d --timeout %d" % (cmd, vmid, tmo)], check=False)
    if pve_status(ip, node, vmid, m["kind"]) == "running":
        if not p.get("force_stop"):
            raise RuntimeError("it did not shut down cleanly on Proxmox; not forcing it")
        log("clean shutdown failed, forcing stop (force_stop was chosen)")
        pve(ip, "%s stop %d" % (cmd, vmid))


def finish_return(m, p, log):
    vmid = m["vmid"]
    net_guest_down(m)
    m["state"] = "returned"
    m["returned"] = int(time.time())
    save_meta(m)
    dnsmasq_lan()
    if p.get("delete_local", True):
        for f in os.listdir(vmdir(vmid)):
            if f.endswith((".raw", ".qcow2", ".fd", ".iso")):
                os.unlink(vmdir(vmid) + "/" + f)
        if m["kind"] == "lxc":
            shutil.rmtree(vmdir(vmid) + "/rootfs", ignore_errors=True)
            shutil.rmtree("%s/%s" % (LXCPATH, ct_name(m)), ignore_errors=True)
        log("deleted the local copy (meta.json and logs kept)")
    log("guest %d is back on %s" % (vmid, m["origin_node"]))


def tunnel(ip, forwards, reverse=False):
    """SSH tunnel of unix sockets to (-L) or from (-R) a Proxmox node."""
    args = []
    for a, b in forwards:
        args += ["-R" if reverse else "-L", "%s:%s" % (a, b)]
    p = subprocess.Popen(pve_ssh_cmd(ip, ["-N", "-o", "ExitOnForwardFailure=yes", "-o", "StreamLocalBindUnlink=yes"] + args),
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    time.sleep(2)
    if p.poll() is not None:
        raise RuntimeError("ssh tunnel failed: %s" % p.stderr.read().decode()[-300:])
    return p


def wait_mirrors(call, jobs, jid, step, log):
    """Wait until every mirror job is ready. If the guest writes faster than the link drains
    (no progress for ~45 s), switch the jobs to write-blocking: each new guest write then goes
    to both sides before it completes, so the mirror is guaranteed to converge."""
    best, stalled, blocking = None, 0, False
    while True:
        js = {j["device"]: j for j in call("query-block-jobs", None)}
        rem = sum(js.get("pp-" + k, {}).get("len", 0) - js.get("pp-" + k, {}).get("offset", 0) for k in jobs)
        job_progress(jid, step=step, done=sum(js.get("pp-" + k, {}).get("offset", 0) for k in jobs),
                     total=max(1, sum(js.get("pp-" + k, {}).get("len", 0) for k in jobs)))
        gone = [k for k in jobs if "pp-" + k not in js]
        if gone:
            raise RuntimeError("mirror job(s) vanished: %s" % gone)
        if all(js["pp-" + k].get("ready") for k in jobs):
            return
        if best is None or rem < best * 0.95:
            best, stalled = rem, 0
        else:
            stalled += 1
        if stalled >= 45 and not blocking:
            for k in jobs:
                try:
                    call("block-job-change", {"id": "pp-" + k, "type": "mirror", "copy-mode": "write-blocking"})
                except Exception as e:
                    log("could not switch %s to write-blocking: %s" % (k, e))
            blocking = True
            log("guest writes as fast as the link drains (%.0f MiB left); mirror switched to write-blocking" % (rem / 2**20))
        time.sleep(1)


def start_migration(call, uri, log):
    """Start a RAM migration with auto-converge on (throttles a write-heavy guest so it can finish
    over a slow link, e.g. Wi-Fi), starting from a 300 ms downtime target."""
    try:
        call("migrate-set-capabilities", {"capabilities": [{"capability": "auto-converge", "state": True}]})
    except Exception as e:
        log("auto-converge not available: %s" % e)
    call("migrate-set-parameters", {"downtime-limit": 300, "cpu-throttle-initial": 20, "cpu-throttle-increment": 10})
    call("migrate", {"uri": uri})


def wait_migration(call, jid, log):
    """Poll a migration; when it stops getting closer, raise the allowed downtime step by step
    (300 ms -> 600 -> ... -> 5 s), like Proxmox does."""
    best, stalled, limit = None, 0, 300
    while True:
        mi = call("query-migrate", None)
        st = mi.get("status")
        ram = mi.get("ram", {})
        job_progress(jid, step="migrate RAM (%s)" % st, done=ram.get("transferred", 0), total=max(1, ram.get("total", 0)),
                     remaining=ram.get("remaining"), throttle=mi.get("cpu-throttle-percentage"))
        if st == "completed":
            return mi
        if st in ("failed", "cancelled"):
            raise RuntimeError("RAM migration %s: %s" % (st, mi.get("error-desc", "")))
        rem = ram.get("remaining")
        if st == "active" and rem is not None:
            if best is None or rem < best * 0.9:
                best, stalled = rem, 0
            else:
                stalled += 1
            if stalled >= 15 and limit < 5000:
                limit = min(5000, limit * 2)
                call("migrate-set-parameters", {"downtime-limit": limit})
                log("migration not converging (%.0f MiB left, CPU throttle %s%%); allowing %d ms downtime"
                    % (rem / 2**20, mi.get("cpu-throttle-percentage", 0), limit))
                stalled = 0
        time.sleep(1)


# ================================================================ VM import
def vm_preflight(cfg, live):
    probs, warn = [], []
    if cfg.get("template"):
        probs.append("it is a template")
    drop, keep = passthrough(cfg)
    if live and (drop or keep):
        probs.append("live moves can't carry passthrough devices (%s); use a cold move" % ", ".join(x.split(" ")[0].rstrip(":") for x in drop + keep))
    else:
        warn += ["dropped here, restored on return: " + x for x in drop] + ["kept: " + x for x in keep]
    if live:
        cpu = cfg.get("cpu", "x86-64-v2-AES").split(",")[0]
        if cpu in ("host", "max"):
            warn.append("CPU type '%s': a live move between different CPUs can crash the guest; needs 'accept CPU risk'. "
                        "A cold move, or a named CPU model such as x86-64-v2-AES, is safe." % cpu)
        if cfg.get("hugepages"):
            probs.append("hugepages are not supported for live moves")
    for k in cfg:
        if re.match(r"^net\d+$", k) and parse_opts(cfg[k])[1].get("bridge") not in C.lan_bridges:
            warn.append("%s is not on a LAN bridge (%s); here it goes to the NAT bridge" % (k, ", ".join(C.lan_bridges)))
    return probs, warn


def pve_pending(ip, node, vmid):
    """Config changes Proxmox has queued for the next restart (the running VM differs from its config)."""
    try:
        return [x["key"] for x in pve_json(ip, "/nodes/%s/qemu/%d/pending" % (node, vmid))
                if "pending" in x or x.get("delete")]
    except Exception:
        return []


def do_import(p, log, jid):
    vmid, node, ip = int(p["vmid"]), p["node"], p["node_ip"]
    live = p.get("move") == "live"
    lk = vm_lock(vmid)
    old = load_json(vmdir(vmid) + "/meta.json")
    if old and old.get("state") in ACTIVE:
        raise RuntimeError("guest %d is already managed here (state %s)" % (vmid, old.get("state")))
    if not os.path.exists(C.kvm):
        raise RuntimeError("Proxmox QEMU bundle missing at %s; run `proxmox-portable fetch-qemu`" % C.pveq)
    log("%s move of VM %d from %s to %s, network %s" % ("LIVE" if live else "cold", vmid, node, C.host_name, p.get("net_mode")))
    cfg = pve_config(ip, node, vmid)
    status = pve_status(ip, node, vmid)
    if live and status != "running":
        raise RuntimeError("a live move needs the VM running on Proxmox (it is %s)" % status)
    probs, warn = vm_preflight(cfg, live)
    if live and pve_pending(ip, node, vmid):
        probs.append("pending config changes (%s) - the running VM no longer matches its config; restart it on "
                     "Proxmox first or use a cold move" % ", ".join(pve_pending(ip, node, vmid)))
    if live and any(w.startswith("CPU type") for w in warn) and not p.get("accept_cpu_risk"):
        probs.append("CPU type host/max needs 'accept CPU risk' for a live move")
    if probs:
        raise RuntimeError("preflight: " + "; ".join(probs))
    for w in warn:
        log("note: " + w)
    if cfg.get("lock"):
        raise RuntimeError("VM is locked on Proxmox (lock: %s)" % cfg["lock"])
    m = common_meta(p, cfg, "qemu")
    m["pve_ha"] = pve_ha(ip, vmid)
    m["imported_from_status"] = status
    need = 0
    for dk in vm_disks(cfg):
        if dk["kind"] == "cdrom":
            log("CD-ROM %s (%s) is left empty here" % (dk["key"], dk["volid"]))
            m["disks"].append({**dk, "base": None})
            continue
        path, fmt, size, used = volume_info(ip, dk["volid"])
        if fmt != "raw":
            raise RuntimeError("%s is %s; only raw volumes (ZFS/LVM zvols, raw files) are supported" % (dk["key"], fmt))
        base = {"disk": "%s.base.raw" % dk["key"], "cloudinit": "%s.iso" % dk["key"], "efi": "efivars.raw",
                "tpm": "tpmstate.raw"}[dk["kind"]]
        dk.update({"pve_path": path, "pve_format": fmt, "size": size, "used": used, "base": base, "overlays": []})
        need += used
        m["disks"].append(dk)
    os.makedirs(C.data, exist_ok=True)
    free = fs_usage(C.data)["free"]
    log("disks need ~%.1f GiB, %.1f GiB free" % (need / 2**30, free / 2**30))
    if need * 1.1 + (2 << 30) > free:
        raise RuntimeError("not enough space: need %.1f GiB, free %.1f GiB" % (need * 1.1 / 2**30, free / 2**30))
    m["pve_argv"] = shlex.split(pve(ip, "qm showcmd %d" % vmid).strip())
    os.makedirs(vmdir(vmid), exist_ok=True)
    save_meta(m)
    if live:
        return import_live(m, p, log, jid, status)
    pve_changed = False
    try:
        pve_changed = ha_release(m, status, log, stop=True, tmo=int(p.get("shutdown_timeout", 300)))
        if pve_status(ip, node, vmid) == "running":
            pve_changed = True
            pve_shutdown(m, p, log)
        park_pve(m, log)
        pve_changed = True
        for dk in m["disks"]:
            if dk.get("base"):
                stream_volume(m, dk, log, jid, "copy " + dk["key"])
        new_overlays(m, time.strftime("%Y%m%d%H%M%S"))
        for dk in m["disks"]:
            if dk["kind"] in ("efi", "tpm"):
                dk["sha_synced"] = sha_file(vmdir(vmid) + "/" + dk["base"])
        m["state"] = "on-host"
        m["imported"] = int(time.time())
        save_meta(m)
    except Exception as e:
        log("move failed: %s" % e)
        m["state"] = "failed-import"
        save_meta(m)
        if pve_changed:
            log("rolling Proxmox back")
            try:
                restore_pve(m, log, start=(status == "running"))
            except Exception as e2:
                log("ROLLBACK FAILED: %s - fix VM %d on %s by hand (tags, onboot, HA)" % (e2, vmid, node))
        raise
    if p.get("start", True):
        guard_pve(m)
        vm_start(m)
        if m.get("guest_ip") and m["net_mode"] == "lan":
            garp(m["guest_ip"])
        log("started VM %d on %s with Proxmox QEMU (machine %s)" % (vmid, C.host_name, m.get("machine")))
    return {"vmid": vmid}


def new_overlays(m, ts):
    d = vmdir(m["vmid"])
    for dk in m["disks"]:
        if dk["kind"] == "disk":
            ov = "%s.ov-%s.qcow2" % (dk["key"], ts)
            backing = dk["overlays"][-1] if dk.get("overlays") else dk["base"]
            run(["bash", "-c", "cd %s && %s create -q -f qcow2 -F %s -b %s %s" % (
                shlex.quote(d), QIMG, "qcow2" if backing.endswith(".qcow2") else "raw", shlex.quote(backing), shlex.quote(ov))])
            dk["overlays"] = dk.get("overlays", []) + [ov]


def snapshot_txn(m, ts, cur_files):
    """QMP transaction: one new qcow2 overlay on top of each disk's current image."""
    d = vmdir(m["vmid"])
    acts, news = [], {}
    for dk in m["disks"]:
        if dk["kind"] == "disk":
            ov = "%s.ov-%s.qcow2" % (dk["key"], ts)
            acts.append({"type": "blockdev-snapshot-sync", "data": {
                "node-name": node_by_file(m["vmid"], d + "/" + cur_files[dk["key"]]), "snapshot-file": d + "/" + ov,
                "snapshot-node-name": "ppov-%s-%s" % (dk["key"], ts), "format": "qcow2"}})
            news[dk["key"]] = ov
    if acts:
        qmp(qmp_path(m["vmid"]), "transaction", {"actions": acts})
    return news


def import_live(m, p, log, jid, status):
    """Run the VM's Proxmox command line here with -incoming, mirror its disks over NBD, migrate RAM,
    switch over - what `qm migrate --online --with-local-disks` does, with this host as target."""
    vmid, ip, node = m["vmid"], m["origin_ip"], m["origin_node"]
    d = vmdir(vmid)
    m["machine"] = pve(ip, "qm status %d --verbose | sed -n 's/^running-machine: //p'" % vmid).strip()
    if not m["machine"]:
        raise RuntimeError("could not read the running machine type")
    log("source runs machine %s" % m["machine"])
    mirrored = [dk for dk in m["disks"] if dk["kind"] in ("disk", "efi")]
    for dk in m["disks"]:
        if dk["kind"] in ("cloudinit", "tpm"):   # TPM contents then travel in the migration stream
            stream_volume(m, dk, log, jid, "copy " + dk["key"])
    # Pre-seed: track writes on the source with a dirty bitmap, then stream each disk compressed
    # while the VM runs (empty space costs ~nothing). The mirror later sends only dirty blocks.
    bitmaps = []
    for dk in mirrored:
        if dk["kind"] == "disk":
            try:
                pve_qmp(ip, vmid, "block-dirty-bitmap-add", {"node": "drive-" + dk["key"], "name": "pp-" + dk["key"]})
                bitmaps.append(dk["key"])
            except Exception as e:
                log("no dirty bitmap for %s (%s); it will be mirrored in full" % (dk["key"], e))
    try:
        for dk in mirrored:
            if dk["key"] in bitmaps:
                stream_volume(m, dk, log, jid, "pre-seed " + dk["key"])
            else:
                with open(d + "/" + dk["base"], "wb") as f:
                    f.truncate(dk["size"])
    except Exception:
        for k in bitmaps:
            try:
                pve_qmp(ip, vmid, "block-dirty-bitmap-remove", {"node": "drive-" + k, "name": "pp-" + k})
            except Exception:
                pass
        raise
    m["seeded"] = bitmaps
    save_meta(m)
    nbd_l, mig_l = "%s/%d-nbd.sock" % (C.run, vmid), "%s/%d-mig.sock" % (C.run, vmid)
    nbd_r, mig_r = "/run/pportable-%d-nbd.sock" % vmid, "/run/pportable-%d-mig.sock" % vmid
    qp = qmp_path(vmid)
    tun, src_paused, ha_removed, jobs, added = None, False, False, [], []
    try:
        os.makedirs(C.run, exist_ok=True)
        for s in (nbd_l, mig_l):
            if os.path.exists(s):
                os.unlink(s)
        vm_start(m, incoming=True)
        qmp(qp, "nbd-server-start", {"addr": {"type": "unix", "data": {"path": nbd_l}}})
        blk = {b.get("device") or b.get("qdev"): b for b in qmp(qp, "query-block")}
        for dk in mirrored:
            nn = "drive-" + dk["key"]
            if dk["kind"] == "efi":
                nn = blk.get("drive-efidisk0", {}).get("inserted", {}).get("node-name") or nn
            qmp(qp, "block-export-add", {"id": "pp-" + dk["key"], "node-name": nn, "writable": True,
                                         "type": "nbd", "name": "drive-" + dk["key"]})
        qmp(qp, "migrate-incoming", {"uri": "unix:" + mig_l})
        pve(ip, "rm -f %s %s" % (nbd_r, mig_r), check=False)
        tun = tunnel(ip, [(nbd_r, nbd_l), (mig_r, mig_l)], reverse=True)
        log("local QEMU waits for the migration; tunnel up")
        ha_removed = ha_release(m, status, log, stop=False)
        for dk in mirrored:
            tgt = "pptgt-" + dk["key"]
            pve_qmp(ip, vmid, "blockdev-add", {"driver": "raw", "node-name": tgt, "file": {
                "driver": "nbd", "server": {"type": "unix", "path": nbd_r}, "export": "drive-" + dk["key"]}})
            added.append(tgt)
            opts = {"job-id": "pp-" + dk["key"], "device": "drive-" + dk["key"], "target": tgt, "sync": "full"}
            if dk["key"] in m.get("seeded", []):
                opts.update(sync="incremental", bitmap="pp-" + dk["key"])
            pve_qmp(ip, vmid, "blockdev-mirror", opts)
            jobs.append(dk["key"])
        log("mirroring %s (%s)" % (", ".join(jobs), "only blocks written since the pre-seed" if m.get("seeded") else "full"))
        wait_mirrors(lambda c, a=None: pve_qmp(ip, vmid, c, a), jobs, jid, "mirror disks", log)
        log("disks in sync; migrating RAM")
        call = lambda c, a=None: pve_qmp(ip, vmid, c, a)
        start_migration(call, "unix:" + mig_r, log)
        mi = wait_migration(call, jid, log)
        src_paused = True
        log("RAM migrated (downtime %s ms); completing disk mirrors" % mi.get("downtime"))
        for k in jobs:
            pve_qmp(ip, vmid, "block-job-cancel", {"device": "pp-" + k})   # on a ready job = clean completion
        for _ in range(120):
            if not any(j["device"].startswith("pp-") for j in pve_qmp(ip, vmid, "query-block-jobs")):
                break
            time.sleep(0.5)
        jobs = []
        for tgt in added:
            pve_qmp(ip, vmid, "blockdev-del", {"node-name": tgt})
        added = []
        for dk in mirrored:
            qmp(qp, "block-export-del", {"id": "pp-" + dk["key"], "mode": "hard"})
        qmp(qp, "nbd-server-stop")
        # base.raw now equals the source disks: start the overlay chain before the guest runs again
        news = snapshot_txn(m, time.strftime("%Y%m%d%H%M%S"), {dk["key"]: dk["base"] for dk in m["disks"] if dk["kind"] == "disk"})
        for dk in m["disks"]:
            if dk["key"] in news:
                dk["overlays"] = [news[dk["key"]]]
        net_guest_up(m)
        qmp(qp, "cont")
        if m.get("guest_ip") and m["net_mode"] == "lan":
            garp(m["guest_ip"])
        log("VM %d is running on %s" % (vmid, C.host_name))
    except Exception as e:
        log("live move failed: %s" % e)
        for k in jobs:
            try:
                pve_qmp(ip, vmid, "block-job-cancel", {"device": "pp-" + k, "force": True})
            except Exception:
                pass
        time.sleep(1)
        for tgt in added:
            try:
                pve_qmp(ip, vmid, "blockdev-del", {"node-name": tgt})
            except Exception:
                pass
        for k in m.get("seeded", []):
            try:
                pve_qmp(ip, vmid, "block-dirty-bitmap-remove", {"node": "drive-" + k, "name": "pp-" + k})
            except Exception:
                pass
        if src_paused:
            try:
                pve_qmp(ip, vmid, "cont")
                log("resumed the VM on %s" % node)
            except Exception as e2:
                log("could not resume on %s: %s" % (node, e2))
        vm_kill(m)
        if ha_removed:
            try:
                restore_pve(m, log, already_running=True)
            except Exception as e2:
                log("HA restore failed: %s" % e2)
        m["state"] = "failed-import"
        save_meta(m)
        raise
    finally:
        if tun:
            tun.terminate()
        pve(ip, "rm -f %s %s" % (nbd_r, mig_r), check=False)
    pve(ip, "qm stop %d --skiplock 1 --timeout 30" % vmid, check=False)   # source sits paused in postmigrate
    park_pve(m, log)
    for dk in m["disks"]:
        if dk["kind"] in ("efi", "tpm"):
            dk["sha_synced"] = None   # the first sync writes them back
    m["state"] = "on-host"
    m["imported"] = int(time.time())
    save_meta(m)
    return {"vmid": vmid, "live": True}


# ================================================================ VM sync
def do_sync(p, log, jid):
    vmid = int(p["vmid"])
    lk = vm_lock(vmid)
    m = load_meta(vmid)
    if m["state"] not in ("on-host", "returning"):
        raise RuntimeError("guest %d state is %s; nothing to sync" % (vmid, m["state"]))
    if m["kind"] == "lxc":
        return ct_sync_locked(m, log, jid)
    return sync_locked(m, log, jid)


def record_sync(m, total, t0, running):
    rec = {"time": int(time.time()), "bytes": total, "seconds": round(time.time() - t0, 1), "ok": True, "running": running}
    m.setdefault("syncs", []).append(rec)
    m["syncs"] = m["syncs"][-200:]
    m["last_sync_ok"] = rec["time"]
    save_meta(m)
    return rec


def sync_locked(m, log, jid):
    vmid, ip, node = m["vmid"], m["origin_ip"], m["origin_node"]
    d = vmdir(vmid)
    t0 = time.time()
    guard_pve(m, offline_ok=False)
    running = vm_state(m) in ("running", "paused")
    ts = time.strftime("%Y%m%d%H%M%S")
    disks = [dk for dk in m["disks"] if dk["kind"] == "disk"]
    if running:
        frozen = False
        try:
            qga(qga_path(vmid), "guest-fsfreeze-freeze", timeout=30)
            frozen = True
        except Exception:
            pass
        try:
            news = snapshot_txn(m, ts, {dk["key"]: dk["overlays"][-1] for dk in disks})
        finally:
            if frozen:
                try:
                    qga(qga_path(vmid), "guest-fsfreeze-thaw", timeout=30)
                except Exception as e:
                    log("WARNING: thaw failed: %s" % e)
        log("froze disks%s" % (" with guest-agent quiesce" if frozen else " (no quiesce: guest agent not answering)"))
        for dk in disks:
            dk["overlays"].append(news[dk["key"]])
        save_meta(m)
    pending = {dk["key"]: (dk["overlays"][:-1] if running else list(dk["overlays"])) for dk in disks}
    total = sum(os.stat(d + "/" + f).st_blocks * 512 for k in pending for f in pending[k])
    log("sync VM %d -> %s: %d overlay file(s), %.1f MiB allocated, guest %s" %
        (vmid, node, sum(len(v) for v in pending.values()), total / 2**20, "running" if running else "stopped"))
    rdir = "%s/%d" % (REMOTE_TMP, vmid)
    pve(ip, "mkdir -p %s" % rdir)
    shipped = 0
    for k, files in pending.items():
        for f in files:
            job_progress(jid, step="ship " + f, done=shipped, total=total)
            p1 = subprocess.Popen(["zstd", "-1", "-T2", "-q", "-c", d + "/" + f], stdout=subprocess.PIPE)
            p2 = subprocess.Popen(pve_ssh_cmd(ip) + ["zstd -d -q -c > %s/%s" % (rdir, f)], stdin=p1.stdout,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            p1.stdout.close()
            _, err = p2.communicate()
            p1.wait()
            if p1.returncode or p2.returncode:
                raise RuntimeError("shipping %s failed: %s" % (f, err.decode(errors="replace")[-400:]))
            shipped += os.stat(d + "/" + f).st_blocks * 512
    # small state (EFI vars, TPM state): written in the same locked window when it changed
    small_cmds, small_new = [], {}
    for dk in m["disks"]:
        if dk["kind"] in ("efi", "tpm") and os.path.exists(d + "/" + dk["base"]):
            data = stable_read(d + "/" + dk["base"])
            h = hashlib.sha256(data).hexdigest()
            if h != dk.get("sha_synced"):
                sp = subprocess.run(pve_ssh_cmd(ip) + ["cat > %s/%s.small" % (rdir, dk["key"])], input=data, capture_output=True)
                if sp.returncode:
                    raise RuntimeError("shipping %s failed" % dk["key"])
                small_cmds.append("dd if=%s.small of=%s bs=64k conv=notrunc status=none && rm -f %s.small" % (
                    dk["key"], shlex.quote(dk["pve_path"]), dk["key"]))
                small_new[dk["key"]] = h
    disk_by = {dk["key"]: dk for dk in disks}
    commits = "\n".join("qemu-img rebase -u -F %s -b %s %s && qemu-img commit -d -q %s && rm -f %s" % (
        disk_by[k]["pve_format"], shlex.quote(disk_by[k]["pve_path"]), f, f, f) for k, files in pending.items() for f in files)
    # on the node: hold pvesr's lock (replication never snapshots a half-written disk) and lock: migrate
    script = r"""
set -e
exec 9>/var/lock/pvesr.lck
flock -w 3600 9
VMID=%(vmid)d
if qm status $VMID | grep -q running; then echo "proxmox-portable: VM is running on Proxmox" >&2; exit 70; fi
for i in $(seq 1 120); do grep -q '^lock:' /etc/pve/qemu-server/$VMID.conf && sleep 15 || break; done
if grep -q '^lock:' /etc/pve/qemu-server/$VMID.conf; then echo "proxmox-portable: config stays locked" >&2; exit 71; fi
qm set $VMID --lock migrate >/dev/null
trap 'qm unlock $VMID' EXIT
cd %(rdir)s
%(commits)s
%(small)s
sync
""" % {"vmid": vmid, "rdir": rdir, "commits": commits, "small": "\n".join(small_cmds)}
    if any(pending.values()) or small_cmds:
        pve(ip, "bash -c %s" % shlex.quote(script), timeout=7200)
    for k, h in small_new.items():
        next(dk for dk in m["disks"] if dk["key"] == k)["sha_synced"] = h
        log("%s changed; written to %s" % (k, node))
    log("committed into %s's disks" % node)
    for dk in disks:
        pend = pending[dk["key"]]
        if not pend:
            continue
        if running:
            jobid = "ppc-%s-%s" % (dk["key"], ts)
            qmp(qmp_path(vmid), "block-commit", {"job-id": jobid, "device": "drive-" + dk["key"],
                                                 "top-node": node_by_file(vmid, d + "/" + pend[-1]),
                                                 "base-node": node_by_file(vmid, d + "/" + dk["base"]),
                                                 "auto-dismiss": False})
            wait_job(vmid, jobid)
            dk["overlays"] = dk["overlays"][len(pend):]
        else:
            run(["bash", "-c", "cd %s && %s commit -q -d -b %s %s" % (shlex.quote(d), QIMG, shlex.quote(dk["base"]), shlex.quote(pend[-1]))])
            dk["overlays"] = []
        for f in pend:
            try:
                os.unlink(d + "/" + f)
            except OSError:
                pass
    if not running:
        new_overlays(m, ts)
    rec = record_sync(m, total, t0, running)
    log("sync done in %.1fs (%.1f MiB)" % (rec["seconds"], total / 2**20))
    return rec


# ================================================================ VM return
def do_return(p, log, jid):
    vmid = int(p["vmid"])
    lk = vm_lock(vmid)
    m = load_meta(vmid)
    if m["state"] != "on-host":
        raise RuntimeError("guest %d state is %s" % (vmid, m["state"]))
    live = p.get("move") == "live"
    log("%s return of VM %d (%s) to %s" % ("LIVE" if live else "cold", vmid, m["name"], m["origin_node"]))
    if live:
        if vm_state(m) != "running":
            raise RuntimeError("a live return needs the VM running on this host")
        return return_live(m, p, log, jid)
    vm_shutdown_wait(m, int(p.get("shutdown_timeout", C.shutdown_timeout)), p.get("force_stop"), log)
    m["state"] = "returning"
    save_meta(m)
    try:
        log("final sync")
        sync_locked(m, log, jid)
        if p.get("verify", True):
            verify(m, log, jid)
    except Exception:
        m["state"] = "on-host"
        save_meta(m)
        raise
    restore_pve(m, log, start=bool(p.get("start", True)))
    finish_return(m, p, log)
    return {"returned": True}


def verify(m, log, jid):
    for dk in [x for x in m["disks"] if x["kind"] == "disk"]:
        job_progress(jid, step="verify " + dk["key"], done=0, total=dk["size"])
        log("verifying %s: sha256 of the local copy vs %s" % (dk["key"], dk["pve_path"]))
        rp = subprocess.Popen(pve_ssh_cmd(m["origin_ip"]) + ["sha256sum %s | cut -c1-64" % shlex.quote(dk["pve_path"])],
                              stdout=subprocess.PIPE, text=True)
        lsha = sha_file(vmdir(m["vmid"]) + "/" + dk["base"])
        rsha = rp.communicate()[0].strip()
        if lsha != rsha:
            raise RuntimeError("verify failed on %s: local %s, Proxmox %s" % (dk["key"], lsha[:16], rsha[:16]))
        log("  %s identical (%s)" % (dk["key"], lsha[:16]))


PVE_START_PY = r'''
import json,re,subprocess,sys
vmid,machine=sys.argv[1],sys.argv[2]
argv=json.loads(sys.argv[3])
for i,a in enumerate(argv):
    if a=="-machine" and machine:
        argv[i+1]=re.sub(r"type=[^,]+","type="+machine,argv[i+1])
argv+=["-incoming","defer","-S"]
subprocess.run(["systemctl","reset-failed","%s.scope"%vmid],capture_output=True)
r=subprocess.run(["systemd-run","--scope","--slice","qemu.slice","--unit",vmid,"-p","KillMode=process",
  "-p","SendSIGKILL=no","--description","Proxmox VE VM %s"%vmid,"--"]+argv,capture_output=True,text=True)
print(json.dumps({"rc":r.returncode,"err":r.stderr[-800:]}))
'''


def return_live(m, p, log, jid):
    """Mirror only the overlay (the changes since the last sync) into the parked disks, which a
    target QEMU on the home node exports over NBD, then migrate RAM back."""
    vmid, ip, node = m["vmid"], m["origin_ip"], m["origin_node"]
    qp = qmp_path(vmid)
    m["state"] = "returning"
    save_meta(m)
    try:
        log("pre-sync: bring the parked disks up to date while the VM keeps running")
        sync_locked(m, log, jid)
    except Exception:
        m["state"] = "on-host"
        save_meta(m)
        raise
    machine = record_machine(m) or m.get("machine")
    log("this host runs machine %s" % machine)
    argv = shlex.split(pve(ip, "qm showcmd %d" % vmid).strip())
    tpm = next((dk for dk in m["disks"] if dk["kind"] == "tpm"), None)
    target_started, tun, jobs, added, src_paused = False, None, [], [], False
    nbd_r, mig_r = "/run/qemu-server/%d_nbd.migrate" % vmid, "/run/qemu-server/%d.migrate" % vmid
    nbd_l, mig_l = "%s/%d-rnbd.sock" % (C.run, vmid), "%s/%d-rmig.sock" % (C.run, vmid)
    mirrored = [dk for dk in m["disks"] if dk["kind"] in ("disk", "efi")]
    try:
        if pve_status(ip, node, vmid) == "running":
            raise RuntimeError("the Proxmox copy is already running")
        if tpm:
            pve(ip, "swtpm socket --tpmstate backend-uri=file://%s,mode=0600 --ctrl type=unixio,path=/var/run/qemu-server/%d.swtpm,mode=0600 "
                    "--pid file=/var/run/qemu-server/%d.swtpm.pid --terminate --daemon --log file=/run/qemu-server/%d-swtpm.log,level=1%s"
                % (shlex.quote(tpm["pve_path"]), vmid, vmid, vmid, " --tpm2" if tpm["opts"].get("version", "v2.0") == "v2.0" else ""))
        r = json.loads(pve(ip, "python3 -c %s %d %s %s" % (shlex.quote(PVE_START_PY), vmid, shlex.quote(machine or ""),
                                                             shlex.quote(json.dumps(argv)))).strip().splitlines()[-1])
        if r["rc"]:
            raise RuntimeError("starting the target on %s failed: %s" % (node, r["err"]))
        target_started = True
        log("target QEMU started on %s (incoming)" % node)
        pve_qmp(ip, vmid, "nbd-server-start", {"addr": {"type": "unix", "data": {"path": nbd_r}}})
        blk = {b.get("device") or b.get("qdev"): b for b in pve_qmp(ip, vmid, "query-block")}
        for dk in mirrored:
            nn = "drive-" + dk["key"]
            if dk["kind"] == "efi":
                nn = blk.get("drive-efidisk0", {}).get("inserted", {}).get("node-name") or nn
            pve_qmp(ip, vmid, "block-export-add", {"id": "pp-" + dk["key"], "node-name": nn, "writable": True,
                                                   "type": "nbd", "name": "drive-" + dk["key"]})
        pve_qmp(ip, vmid, "migrate-incoming", {"uri": "unix:" + mig_r})
        for s in (nbd_l, mig_l):
            if os.path.exists(s):
                os.unlink(s)
        tun = tunnel(ip, [(nbd_l, nbd_r), (mig_l, mig_r)], reverse=False)
        for dk in mirrored:
            tgt = "pptgt-" + dk["key"]
            qmp(qp, "blockdev-add", {"driver": "raw", "node-name": tgt, "file": {
                "driver": "nbd", "server": {"type": "unix", "path": nbd_l}, "export": "drive-" + dk["key"]}})
            added.append(tgt)
            qmp(qp, "blockdev-mirror", {"job-id": "pp-" + dk["key"], "device": "drive-" + dk["key"], "target": tgt,
                                        "sync": "top" if dk["kind"] == "disk" else "full"})
            jobs.append(dk["key"])
        wait_mirrors(lambda c, a=None: qmp(qp, c, a), jobs, jid, "mirror changes", log)
        log("changes mirrored; migrating RAM")
        call = lambda c, a=None: qmp(qp, c, a)
        start_migration(call, "unix:" + mig_l, log)
        mi = wait_migration(call, jid, log)
        src_paused = True
        log("RAM migrated (downtime %s ms)" % mi.get("downtime"))
        for k in jobs:
            qmp(qp, "block-job-cancel", {"device": "pp-" + k})
        for _ in range(120):
            if not any(j["device"].startswith("pp-") for j in qmp(qp, "query-block-jobs")):
                break
            time.sleep(0.5)
        jobs = []
        for tgt in added:          # drop our NBD client connections before removing the exports
            qmp(qp, "blockdev-del", {"node-name": tgt})
        added = []
        for dk in mirrored:
            pve_qmp(ip, vmid, "block-export-del", {"id": "pp-" + dk["key"], "mode": "hard"})
        pve_qmp(ip, vmid, "nbd-server-stop")
        pve_qmp(ip, vmid, "cont")
        log("VM %d is running on %s again" % (vmid, node))
    except Exception as e:
        log("live return failed: %s" % e)
        for k in jobs:
            try:
                qmp(qp, "block-job-cancel", {"device": "pp-" + k, "force": True})
            except Exception:
                pass
        for tgt in added:
            try:
                qmp(qp, "blockdev-del", {"node-name": tgt})
            except Exception:
                pass
        if target_started:
            pve(ip, "qm stop %d --skiplock 1" % vmid, check=False)
            log("stopped the half-started target on %s" % node)
        if src_paused:
            try:
                qmp(qp, "cont")
                log("resumed the VM on this host")
            except Exception as e2:
                log("could not resume on this host: %s" % e2)
        m["state"] = "on-host"
        save_meta(m)
        raise
    finally:
        if tun:
            tun.terminate()
    vm_kill(m)
    restore_pve(m, log, start=True, already_running=True)
    finish_return(m, p, log)
    return {"returned": True, "live": True}


# ================================================================ containers (LXC)
def ct_name(m):
    return "pve-%d" % m["vmid"]


def ct_state(m):
    s = run(["lxc-info", "-n", ct_name(m), "-sH"], check=False).strip().lower()
    return {"frozen": "paused"}.get(s, s or "stopped")


def ct_status(m):
    if ct_state(m) != "running":
        return {}
    o = run(["lxc-info", "-n", ct_name(m)], check=False)
    return {"ips": {"source": "lxc", "ips": [x for x in re.findall(r"^IP:\s+(\S+)", o, re.M) if ":" not in x]},
            "stats": dict(re.findall(r"^(CPU use|Memory use|TX bytes|RX bytes|PID):\s+(.*)$", o, re.M))}


def ct_power(m, action):
    n = ct_name(m)
    if action == "start":
        guard_pve(m)
        net_guest_up(m)
        run(["lxc-start", "-n", n])
        if m.get("guest_ip") and m["net_mode"] == "lan":
            garp(m["guest_ip"])
    elif action == "shutdown":
        run(["lxc-stop", "-n", n, "-t", "120"])
    elif action == "stop":
        run(["lxc-stop", "-n", n, "-k"])
    elif action == "reboot":
        run(["lxc-stop", "-n", n, "-r", "-t", "120"])


def ct_rootfs(m):
    return vmdir(m["vmid"]) + "/rootfs"


def ct_preflight(cfg):
    probs, warn = [], []
    if cfg.get("template"):
        probs.append("it is a template")
    for k, v in sorted(cfg.items()):
        if MP_RE.match(k):
            vol, o = parse_opts(v)
            if vol.startswith("/"):
                warn.append("%s is a bind mount of host path %s: not copied, empty here" % (k, vol))
        if re.match(r"^dev\d+$", k):
            warn.append("%s (%s): device passthrough stays in the node" % (k, v))
        if k.startswith("lxc."):
            warn.append("raw LXC key %s is not carried over" % k)
    if cfg.get("arch", "amd64") != "amd64":
        probs.append("arch %s" % cfg.get("arch"))
    return probs, warn


def ct_bind_excludes(cfg):
    ex = []
    for k, v in cfg.items():
        if MP_RE.match(k):
            vol, o = parse_opts(v)
            if vol.startswith("/") and o.get("mp"):
                ex += ["--exclude", o["mp"].rstrip("/") + "/***"]
    return ex


def ct_write_config(m):
    cfg = m["pve_config"]
    unpriv = str(cfg.get("unprivileged", "0")) == "1"
    feats = dict(x.split("=", 1) for x in cfg.get("features", "").split(",") if "=" in x)
    lines = ["# proxmox-portable: CT %d (%s) from %s. Generated, do not edit." % (m["vmid"], m["name"], m["origin_node"]),
             "lxc.include = /usr/share/lxc/config/common.conf"]
    if unpriv:
        lines += ["lxc.include = /usr/share/lxc/config/userns.conf", "lxc.idmap = u 0 100000 65536", "lxc.idmap = g 0 100000 65536"]
    if feats.get("nesting") == "1":
        lines.append("lxc.include = /usr/share/lxc/config/nesting.conf")
    lines += ["lxc.arch = amd64", "lxc.uts.name = %s" % cfg.get("hostname", m["name"]),
              "lxc.rootfs.path = dir:%s" % ct_rootfs(m), "lxc.apparmor.profile = generated",
              "lxc.apparmor.allow_nesting = %d" % (1 if feats.get("nesting") == "1" else 0),
              "lxc.cgroup2.memory.max = %dM" % int(cfg.get("memory", 512)),
              "lxc.cgroup2.memory.swap.max = %dM" % int(cfg.get("swap", 0)),
              "lxc.cgroup2.cpu.max = %d 100000" % (int(cfg.get("cores", 1)) * 100000),
              "lxc.start.auto = 0", "lxc.tty.max = %d" % int(cfg.get("tty", 2))]
    n = 0
    for k in sorted(x for x in cfg if re.match(r"^net\d+$", x)):
        vol, o = parse_opts(cfg[k])
        o.setdefault("name", o.get("_first_val") if o.get("_first_key") == "name" else "eth%d" % n)
        lines += ["lxc.net.%d.type = veth" % n, "lxc.net.%d.link = %s" % (n, m["bridges"].get(k, C.nat_bridge)),
                  "lxc.net.%d.flags = up" % n, "lxc.net.%d.name = %s" % (n, o["name"]),
                  "lxc.net.%d.veth.pair = veth%di%d" % (n, m["vmid"], n)]
        if o.get("hwaddr"):
            lines.append("lxc.net.%d.hwaddr = %s" % (n, o["hwaddr"].lower()))
        if o.get("mtu"):
            lines.append("lxc.net.%d.mtu = %s" % (n, o["mtu"]))
        n += 1
    os.makedirs("%s/%s" % (LXCPATH, ct_name(m)), exist_ok=True)
    with open("%s/%s/config" % (LXCPATH, ct_name(m)), "w") as f:
        f.write("\n".join(lines) + "\n")


RSYNC = ["rsync", "-aHAX", "--numeric-ids", "--delete", "--inplace", "--stats"]


def ct_rsync(m, src, dst, log, label):
    e = " ".join(shlex.quote(x) for x in pve_ssh_cmd(m["origin_ip"])[:-1])
    t0 = time.time()
    o = run(RSYNC + ct_bind_excludes(m["pve_config"]) + ["-e", e, src, dst], timeout=86400)
    moved = re.search(r"Total bytes %s: ([\d,]+)" % ("received" if src.startswith("root@") else "sent"), o)
    n = int(moved.group(1).replace(",", "")) if moved else 0
    log("  %s: %.1f MiB in %.0fs" % (label, n / 2**20, time.time() - t0))
    return n


def ct_live_path(m):
    """Directory of the CT's rootfs on the node while it runs (ZFS/dir subvols), else None."""
    vol, o = parse_opts(m["pve_config"]["rootfs"])
    try:
        path = pve(m["origin_ip"], "pvesm path %s" % shlex.quote(vol)).strip()
    except Exception:
        return None
    if path and pve(m["origin_ip"], "test -d %s && echo yes" % shlex.quote(path), check=False).strip() == "yes":
        return path
    return None


def ct_import(p, log, jid):
    vmid, node, ip = int(p["vmid"]), p["node"], p["node_ip"]
    lk = vm_lock(vmid)
    old = load_json(vmdir(vmid) + "/meta.json")
    if old and old.get("state") in ACTIVE:
        raise RuntimeError("guest %d is already managed here" % vmid)
    if not shutil.which("lxc-start"):
        raise RuntimeError("lxc is not installed on this host; run `proxmox-portable setup`")
    cfg = pve_config(ip, node, vmid, "lxc")
    status = pve_status(ip, node, vmid, "lxc")
    probs, warn = ct_preflight(cfg)
    if probs:
        raise RuntimeError("preflight: " + "; ".join(probs))
    for w in warn:
        log("note: " + w)
    if cfg.get("lock"):
        raise RuntimeError("CT is locked on Proxmox (lock: %s)" % cfg["lock"])
    m = common_meta(p, cfg, "lxc")
    m["pve_ha"] = pve_ha(ip, vmid, "lxc")
    m["imported_from_status"] = status
    m["move_type"] = "restart"
    os.makedirs(ct_rootfs(m), exist_ok=True)
    save_meta(m)
    log("move of CT %d (%s) from %s, network %s" % (vmid, m["name"], node, m["net_mode"]))
    pve_changed = mounted = False
    remote = "root@%s:" % ip
    try:
        live = ct_live_path(m) if status == "running" else None
        if live:
            log("pre-copy while the CT keeps running on %s (%s)" % (node, live))
            job_progress(jid, step="pre-copy rootfs", done=0, total=1)
            ct_rsync(m, remote + live + "/", ct_rootfs(m) + "/", log, "pre-copy")
        pve_changed = ha_release(m, status, log, stop=True, tmo=int(p.get("shutdown_timeout", 300)))
        if pve_status(ip, node, vmid, "lxc") == "running":
            pve_changed = True
            pve_shutdown(m, p, log)
        park_pve(m, log)
        pve_changed = True
        pve(ip, "pct mount %d" % vmid)
        mounted = True
        job_progress(jid, step="final copy", done=0, total=1)
        ct_rsync(m, remote + "/var/lib/lxc/%d/rootfs/" % vmid, ct_rootfs(m) + "/", log, "final copy (CT stopped)" if live else "copy")
        pve(ip, "pct unmount %d" % vmid)
        mounted = False
        ct_write_config(m)
        m["state"] = "on-host"
        m["imported"] = int(time.time())
        save_meta(m)
    except Exception as e:
        log("move failed: %s" % e)
        if mounted:
            pve(ip, "pct unmount %d" % vmid, check=False)
        m["state"] = "failed-import"
        save_meta(m)
        if pve_changed:
            try:
                restore_pve(m, log, start=(status == "running"))
            except Exception as e2:
                log("ROLLBACK FAILED: %s" % e2)
        raise
    if p.get("start", True):
        guard_pve(m)
        net_guest_up(m)
        run(["lxc-start", "-n", ct_name(m)])
        if m.get("guest_ip") and m["net_mode"] == "lan":
            garp(m["guest_ip"])
        log("started CT %d on %s" % (vmid, C.host_name))
    return {"vmid": vmid}


def ct_sync_locked(m, log, jid):
    """rsync the CT into its parked rootfs: pass 1 with the CT running, pass 2 frozen."""
    vmid, ip, node = m["vmid"], m["origin_ip"], m["origin_node"]
    t0 = time.time()
    guard_pve(m, offline_ok=False)
    running = ct_state(m) == "running"
    # one ssh session holds the pvesr lock and the pct mount for the whole sync; closing its stdin ends it
    hold = r"""
set -e
exec 9>/var/lock/pvesr.lck
flock -w 3600 9
if pct status %(v)d | grep -q running; then echo "proxmox-portable: CT is running on Proxmox" >&2; exit 70; fi
pct mount %(v)d >/dev/null
echo MOUNTED
read -r _ || true
pct unmount %(v)d || true
pct unlock %(v)d || true
""" % {"v": vmid}
    holder = subprocess.Popen(pve_ssh_cmd(ip) + ["bash -c %s" % shlex.quote(hold)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if "MOUNTED" not in holder.stdout.readline():
        holder.kill()
        raise RuntimeError("could not lock/mount CT %d on %s: %s" % (vmid, node, holder.stderr.read()[-400:]))
    dst = "root@%s:/var/lib/lxc/%d/rootfs/" % (ip, vmid)
    total = 0
    try:
        if running:
            total += ct_rsync(m, ct_rootfs(m) + "/", dst, log, "pass 1 (running)")
            run(["lxc-freeze", "-n", ct_name(m)])
            try:
                total += ct_rsync(m, ct_rootfs(m) + "/", dst, log, "pass 2 (frozen)")
            finally:
                run(["lxc-unfreeze", "-n", ct_name(m)], check=False)
        else:
            total += ct_rsync(m, ct_rootfs(m) + "/", dst, log, "copy (stopped)")
    finally:
        try:
            holder.stdin.close()
            holder.wait(timeout=60)
        except Exception:
            holder.kill()
    rec = record_sync(m, total, t0, running)
    log("sync done in %.1fs" % rec["seconds"])
    return rec


def ct_return(p, log, jid):
    vmid = int(p["vmid"])
    lk = vm_lock(vmid)
    m = load_meta(vmid)
    if m["state"] != "on-host":
        raise RuntimeError("guest %d state is %s" % (vmid, m["state"]))
    log("return of CT %d (%s) to %s" % (vmid, m["name"], m["origin_node"]))
    m["state"] = "returning"
    save_meta(m)
    try:
        if ct_state(m) == "running":
            log("pre-sync while the CT keeps running")
            ct_sync_locked(m, log, jid)
            log("stopping the CT on this host")
            subprocess.run(["lxc-stop", "-n", ct_name(m), "-t", str(int(p.get("shutdown_timeout", 120)))], capture_output=True)
            if ct_state(m) == "running":
                if not p.get("force_stop"):
                    raise RuntimeError("CT did not stop; not forcing it")
                run(["lxc-stop", "-n", ct_name(m), "-k"])
        log("final sync (CT stopped)")
        ct_sync_locked(m, log, jid)
    except Exception:
        m["state"] = "on-host"
        save_meta(m)
        raise
    restore_pve(m, log, start=bool(p.get("start", True)))
    finish_return(m, p, log)
    return {"returned": True}


# ================================================================ sync schedule
def cmd_set_sync(args):
    vmid, iv = int(args[0]), args[1]
    if iv not in INTERVALS and iv != "off":
        raise UserError("interval must be one of " + ", ".join(list(INTERVALS) + ["off"]))
    lk = vm_lock(vmid)
    m = load_meta(vmid)
    m["sync_interval"] = iv
    save_meta(m)
    return {"ok": True, "sync_interval": iv}


def sync_due():
    """Start a sync job for every guest whose interval has passed since its last sync or attempt."""
    now = time.time()
    for m in managed():
        iv = INTERVALS.get(m.get("sync_interval") or "off")
        if m.get("state") != "on-host" or not iv:
            continue
        mark = vmdir(m["vmid"]) + "/.last_sync_try"
        try:
            last = max(float(readf(mark, "0") or 0), (m.get("syncs") or [{}])[-1].get("time", 0), m.get("imported", 0))
        except ValueError:
            last = 0
        if now - last < iv or job_running(m["vmid"]):
            continue
        with open(mark, "w") as f:
            f.write(str(int(now)))
        try:
            new_job("sync", m["vmid"], {"vmid": m["vmid"], "kind": m["kind"]}, timer=True)
        except Exception as e:
            print("scheduled sync of %d: %s" % (m["vmid"], e), file=sys.stderr)


# ================================================================ sampling (graphs)
def rrd_push(name, sample):
    os.makedirs(C.rrd, exist_ok=True)
    p = C.rrd + "/" + name + ".json"
    write_json(p, [s for s in load_json(p, []) + [sample] if s["t"] > sample["t"] - 86400])


def net_bytes():
    rx = tx = 0
    for line in readf("/proc/net/dev").splitlines()[2:]:
        name, rest = line.split(":", 1)
        name = name.strip()
        if name == "lo" or name.startswith(("vnet", "virbr", C.bridge, C.nat_bridge, "docker", "veth", "tailscale", "tap")):
            continue
        f = rest.split()
        rx += int(f[0])
        tx += int(f[8])
    return rx, tx


def disk_bytes():
    r = w = 0
    for line in readf("/proc/diskstats").splitlines():
        f = line.split()
        if re.match(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|mmcblk\d+)$", f[2]):
            r += int(f[5]) * 512
            w += int(f[9]) * 512
    return r, w


def cgroup_ct(m):
    for p in ("/sys/fs/cgroup/lxc.payload.%s" % ct_name(m), "/sys/fs/cgroup/lxc.payload/%s" % ct_name(m)):
        if os.path.isdir(p):
            return p
    return None


def sample():
    now = int(time.time())
    state = load_json(C.rrd + "/.state.json", {})
    tot, idle = cpu_times()
    rx, tx = net_bytes()
    dr, dw = disk_bytes()
    mi = meminfo()
    s = {"t": now, "mem": mi["MemTotal"] - mi["MemAvailable"], "memtotal": mi["MemTotal"], "load": float(readf("/proc/loadavg").split()[0])}
    pv = state.get("host")
    if pv and now > pv["t"]:
        dt = now - pv["t"]
        s.update(cpu=max(0, 1 - (idle - pv["idle"]) / max(1, tot - pv["tot"])), netin=max(0, (rx - pv["rx"]) / dt),
                 netout=max(0, (tx - pv["tx"]) / dt), diskread=max(0, (dr - pv["dr"]) / dt), diskwrite=max(0, (dw - pv["dw"]) / dt))
        rrd_push("host", s)
    state["host"] = {"t": now, "tot": tot, "idle": idle, "rx": rx, "tx": tx, "dr": dr, "dw": dw}
    hz = os.sysconf("SC_CLK_TCK")
    for m in managed():
        key = "vm%d" % m["vmid"]
        cur, mem = None, 0
        if m["kind"] == "qemu" and vm_pid(m["vmid"]):
            st = vm_stats(m)
            cur = {"t": now, "cpu": st.get("cpu_ticks", 0) / hz, "rx": st["rx"], "tx": st["tx"], "rd": st["rd"], "wr": st["wr"]}
            mem = st.get("mem_used") or st.get("rss", 0)
        elif m["kind"] == "lxc" and cgroup_ct(m):
            cg = cgroup_ct(m)
            usec = int((re.search(r"usage_usec (\d+)", readf(cg + "/cpu.stat")) or [0, 0])[1])
            io = readf(cg + "/io.stat")
            rxb = txb = 0
            for n in os.listdir("/sys/class/net"):
                if n.startswith("veth%di" % m["vmid"]):
                    txb += int(readf("/sys/class/net/%s/statistics/rx_bytes" % n, "0"))
                    rxb += int(readf("/sys/class/net/%s/statistics/tx_bytes" % n, "0"))
            cur = {"t": now, "cpu": usec / 1e6, "rx": rxb, "tx": txb,
                   "rd": sum(int(x) for x in re.findall(r"rbytes=(\d+)", io)), "wr": sum(int(x) for x in re.findall(r"wbytes=(\d+)", io))}
            mem = int(readf(cg + "/memory.current", "0") or 0)
        if not cur:
            state.pop(key, None)
            continue
        pv = state.get(key)
        if pv and now > pv["t"]:
            dt = now - pv["t"]
            rrd_push(key, {"t": now, "cpu": max(0, (cur["cpu"] - pv["cpu"]) / dt / max(1, m.get("vcpus") or 1)),
                           "mem": mem, "memtotal": m.get("memory_mb", 0) * 2**20,
                           "netin": max(0, (cur["rx"] - pv["rx"]) / dt), "netout": max(0, (cur["tx"] - pv["tx"]) / dt),
                           "diskread": max(0, (cur["rd"] - pv["rd"]) / dt), "diskwrite": max(0, (cur["wr"] - pv["wr"]) / dt)})
        state[key] = cur
    os.makedirs(C.rrd, exist_ok=True)
    write_json(C.rrd + "/.state.json", state)


def cmd_history(args=None):
    return {"ok": True, "data": load_json(C.rrd + "/" + ("host" if not args else "vm%d" % int(args[0])) + ".json", [])}


def cmd_boot(args=None):
    """Once per boot: networking, then start guests that had onboot=1 on Proxmox."""
    net_up()
    started = []
    for m in managed():
        if m.get("state") != "on-host" or str(m.get("pve_onboot")) != "1":
            continue
        try:
            guard_pve(m)
            if m["kind"] == "lxc":
                if ct_state(m) != "running":
                    ct_power(m, "start")
            elif not vm_pid(m["vmid"]):
                vm_start(m)
            started.append(m["vmid"])
        except Exception as e:
            print("not starting %d: %s" % (m["vmid"], e), file=sys.stderr)
    return {"ok": True, "started": started}


# ================================================================ moves: entry points
def start_import(p):
    vmid = int(p["vmid"])
    if not re.match(r"^[0-9a-fA-F.:]+$", p.get("node_ip", "")) or not re.match(r"^[A-Za-z0-9-]+$", p.get("node", "")):
        raise UserError("bad node/node_ip")
    if p.get("guest_ip") and not re.match(r"^\d+\.\d+\.\d+\.\d+$", p["guest_ip"]):
        raise UserError("bad guest_ip")
    if p.get("sync_interval") not in INTERVALS and p.get("sync_interval") != "off":
        p["sync_interval"] = C.sync_default
    p["kind"] = p.get("kind") if p.get("kind") in ("qemu", "lxc") else "qemu"
    m = load_json(vmdir(vmid) + "/meta.json")
    if m and m.get("state") in ACTIVE:
        raise UserError("guest %d is already here (state %s)" % (vmid, m["state"]))
    if job_running(vmid):
        raise UserError("a job for %d is already running" % vmid)
    return {"ok": True, "job": new_job("import", vmid, p)}


def cmd_sync(args):
    m = load_meta(int(args[0]))
    if job_running(m["vmid"]):
        raise UserError("a job for %d is already running" % m["vmid"])
    return {"ok": True, "job": new_job("sync", m["vmid"], {"vmid": m["vmid"], "kind": m["kind"]})}


def start_return(p):
    m = load_meta(int(p["vmid"]))
    if m["state"] != "on-host":
        raise UserError("guest %d state is %s" % (m["vmid"], m["state"]))
    if job_running(m["vmid"]):
        raise UserError("a job for %d is already running" % m["vmid"])
    p["kind"] = m["kind"]
    return {"ok": True, "job": new_job("return", m["vmid"], p)}


def cmd_preflight(node, ip, vmid, kind):
    """Can this host take the guest, cold and live?"""
    try:
        cfg = pve_config(ip, node, vmid, kind)
        status = pve_status(ip, node, vmid, kind)
    except Exception as e:
        raise UserError("cannot reach %s over SSH as root: %s" % (ip, e))
    os.makedirs(C.data, exist_ok=True)
    free = fs_usage(C.data)["free"]
    res = {"ok": True, "host": C.host_name, "kind": kind, "status": status, "cpu_vendor": cpuinfo("vendor_id"), "free": free}
    if kind == "lxc":
        probs, warn = ct_preflight(cfg)
        if not shutil.which("lxc-start"):
            probs.append("lxc is not installed on this host")
        vol, o = parse_opts(cfg.get("rootfs", ""))
        need = 0
        try:
            path = pve(ip, "pvesm path %s" % shlex.quote(vol)).strip()
            if pve(ip, "test -d %s && echo d" % shlex.quote(path), check=False).strip() == "d":
                need = int(pve(ip, "du -sxb %s | cut -f1" % shlex.quote(path)).strip())
        except Exception:
            pass
        if not need:
            sz = o.get("size", "8G")
            need = int(float(re.sub(r"[^0-9.]", "", sz) or 8) * {"G": 2**30, "T": 2**40, "M": 2**20}.get(sz[-1:], 2**30))
        disks = [{"key": "rootfs", "volid": vol, "used": need, "action": "rsync (file level, deltas on every sync)"}]
        for k in sorted(cfg):
            if MP_RE.match(k):
                v2, o2 = parse_opts(cfg[k])
                disks.append({"key": k, "volid": v2, "action": "skipped (host bind mount)" if v2.startswith("/") else "rsync with the rootfs"})
        if need * 1.1 + (1 << 30) > free:
            probs.append("not enough space: need %.1f GiB" % (need * 1.1 / 2**30))
        res.update(problems=probs, warnings=warn, disks=disks, need=need, live_ok=False,
                   live_problems=["containers can't be live-migrated (Proxmox can't either); they move in restart mode: "
                                  "pre-copy while running, then a short stop for the final delta"], live_warnings=[])
        return res
    probs, warn = vm_preflight(cfg, False)
    lprobs, lwarn = vm_preflight(cfg, True)
    if not os.path.exists(C.kvm):
        probs.append("Proxmox QEMU bundle missing on this host (run `proxmox-portable fetch-qemu`)")
    need = 0
    disks = []
    for dk in vm_disks(cfg):
        if dk["kind"] == "cdrom":
            disks.append({"key": dk["key"], "volid": dk["volid"], "action": "left empty (CD-ROM)"})
            continue
        try:
            path, fmt, size, used = volume_info(ip, dk["volid"])
            if fmt != "raw":
                probs.append("%s is %s (only raw/ZFS/LVM volumes)" % (dk["key"], fmt))
            need += used
            disks.append({"key": dk["key"], "volid": dk["volid"], "path": path, "size": size, "used": used, "format": fmt,
                          "action": {"disk": "block copy, then changed-block sync", "efi": "copy (EFI vars)",
                                     "tpm": "copy (TPM state)", "cloudinit": "copy (cloud-init)"}[dk["kind"]]})
        except Exception as e:
            probs.append("%s: %s" % (dk["key"], e))
    if need * 1.1 + (2 << 30) > free:
        probs.append("not enough space: need %.1f GiB, %.1f GiB free" % (need * 1.1 / 2**30, free / 2**30))
    vc = int(cfg.get("sockets", 1)) * int(cfg.get("cores", 1))
    if vc > os.cpu_count():
        probs.append("VM has %d vCPUs, this host has %d" % (vc, os.cpu_count()))
    if int(str(cfg.get("memory", 0))) * 2**20 > meminfo()["MemTotal"] * 0.8:
        warn.append("VM memory is more than 80% of this host's RAM")
    if status != "running":
        lprobs.append("the VM is not running (a live move needs it running)")
    elif pve_pending(ip, node, vmid):
        lprobs.append("pending config changes (%s): restart it on Proxmox first, or use a cold move"
                      % ", ".join(pve_pending(ip, node, vmid)))
    res.update(problems=probs, warnings=warn, disks=disks, need=need, live_ok=not probs and not lprobs,
               live_problems=lprobs, live_warnings=[w for w in lwarn if w not in warn], cpu=cfg.get("cpu", "x86-64-v2-AES"))
    return res


# ================================================================ setup / fetch-qemu / check
def deb_installed(pkg):
    # dpkg-query copes with packages installed for several architectures (dpkg -s calls those ambiguous)
    o = subprocess.run(["dpkg-query", "-W", "-f=${db:Status-Abbrev}\\n", pkg], capture_output=True, text=True).stdout
    return any(line.startswith("ii") for line in o.splitlines())


def cmd_setup(args):
    """Install packages, create directories and keys, write and enable the systemd unit."""
    need_root()
    missing = [p for p in DEB_PKGS if not deb_installed(p)]
    if missing:
        print("installing: " + " ".join(missing), flush=True)
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        subprocess.run(["apt-get", "update", "-qq"], check=True, env=env)
        subprocess.run(["apt-get", "install", "-y", "-qq", "--no-install-recommends"] + missing, check=True, env=env)
    for d in (C.etc, C.data, C.vms, C.jobs, C.rrd, C.helpers):
        os.makedirs(d, exist_ok=True)
    os.chmod(C.etc, 0o700)
    os.chmod(C.path, 0o600)
    # unprivileged Proxmox containers map root to 100000
    for f in ("/etc/subuid", "/etc/subgid"):
        if "root:100000:65536" not in readf(f).split():
            with open(f, "a") as fh:
                fh.write("root:100000:65536\n")
    if not os.path.exists(C.ssh_key):
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "proxmox-portable@" + C.host_name, "-f", C.ssh_key])
    open(C.ssh_known, "a").close()
    write_helpers()
    unit = """[Unit]
Description=proxmox-portable: Proxmox-style console and guest host (port %d)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%s %s --config %s serve
Restart=on-failure
RestartSec=5
KillMode=process

[Install]
WantedBy=multi-user.target
""" % (C.port, sys.executable, SELF, C.path)
    with open("/etc/systemd/system/proxmox-portable.service", "w") as f:
        f.write(unit)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "proxmox-portable.service"])
    print("\nsetup done.\n\nAdd this public key to the cluster (on ANY Proxmox node, as root):\n")
    print("  echo '%s' >> /etc/pve/priv/authorized_keys\n" % readf(C.ssh_key + ".pub").strip())
    print("Then:  %s fetch-qemu        (copies Proxmox's QEMU from a node)" % os.path.basename(SELF))
    print("       %s check             (tests everything)" % os.path.basename(SELF))
    print("       systemctl start proxmox-portable   ->  https://<this host>:%d/" % C.port)
    return None


def cmd_fetch_qemu(args):
    """Download Proxmox's QEMU, swtpm and OVMF on a node (same version as the cluster) and unpack them
    beside Debian's packages (they conflict as packages)."""
    need_root()
    ip = args[0] if args else None
    if not ip:
        ips = [v for v in node_ips().values() if v]
        if not ips:
            raise UserError("no node IP found; pass one: fetch-qemu NODE_IP")
        ip = ips[0]
    pveq, new = C.pveq, C.pveq + ".new"
    rtmp = pve(ip, "mktemp -d /var/tmp/proxmox-portable-debs.XXXXXX").strip()
    try:
        print("downloading %s on %s" % (" ".join(PVE_DEBS), ip), flush=True)
        pve(ip, "cd %s && apt-get download %s >/dev/null 2>&1; ls *.deb | wc -l" % (rtmp, " ".join(PVE_DEBS)), timeout=600)
        shutil.rmtree(new, ignore_errors=True)
        os.makedirs(new + "/debs")
        os.makedirs(new + "/root")
        p1 = subprocess.Popen(pve_ssh_cmd(ip) + ["tar -C %s -cf - ." % rtmp], stdout=subprocess.PIPE)
        subprocess.run(["tar", "-C", new + "/debs", "-xf", "-"], stdin=p1.stdout, check=True)
        p1.wait()
        debs = sorted(f for f in os.listdir(new + "/debs") if f.endswith(".deb"))
        if not any(d.startswith("pve-qemu-kvm_") for d in debs):
            raise UserError("pve-qemu-kvm did not download on %s (is it a Proxmox node with working apt?)" % ip)
        for d in debs:
            run(["dpkg", "-x", new + "/debs/" + d, new + "/root"])
        os.makedirs("/usr/share/qemu-server", exist_ok=True)
        p1 = subprocess.Popen(pve_ssh_cmd(ip) + ["tar -C /usr/share/qemu-server -cf - ."], stdout=subprocess.PIPE)
        subprocess.run(["tar", "-C", "/usr/share/qemu-server", "-xf", "-"], stdin=p1.stdout, check=True)
        p1.wait()
    finally:
        pve(ip, "rm -rf %s" % shlex.quote(rtmp), check=False)
    shutil.rmtree(pveq + ".old", ignore_errors=True)
    if os.path.exists(pveq):
        os.rename(pveq, pveq + ".old")
    os.rename(new, pveq)
    shutil.rmtree(pveq + ".old", ignore_errors=True)
    r = pveq + "/root"
    libs = ":".join(r + x for x in ("/usr/lib", "/usr/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu/swtpm",
                                    "/usr/lib/ceph", "/lib/x86_64-linux-gnu", "/lib"))
    os.makedirs(pveq + "/bin", exist_ok=True)
    # Proxmox's QEMU uses KVM only when argv[0] is `kvm`: the wrapper must exec usr/bin/kvm
    for name, binary in (("kvm", "kvm"), ("swtpm", "swtpm"), ("swtpm_setup", "swtpm_setup"), ("qemu-img-pve", "qemu-img")):
        with open("%s/bin/%s" % (pveq, name), "w") as f:
            f.write("#!/bin/sh\nexport LD_LIBRARY_PATH=%s\nexec %s/usr/bin/%s \"$@\"\n" % (libs, r, binary))
        os.chmod("%s/bin/%s" % (pveq, name), 0o755)
    # Proxmox's command lines name firmware under these paths
    for link, target in (("/usr/share/kvm", r + "/usr/share/kvm"), ("/usr/share/pve-edk2-firmware", r + "/usr/share/pve-edk2-firmware")):
        if os.path.islink(link) or not os.path.exists(link):
            if os.path.islink(link):
                os.unlink(link)
            os.symlink(target, link)
        else:
            print("WARNING: %s exists and is not a symlink; left alone (Proxmox firmware may not be found)" % link)
    miss = run(["sh", "-c", "LD_LIBRARY_PATH=%s ldd %s/usr/bin/qemu-system-x86_64 %s/usr/bin/swtpm | grep 'not found' | sort -u"
                % (libs, r, r)], check=False).strip()
    if miss:
        raise UserError("missing libraries for the Proxmox QEMU (run `setup` first?):\n" + miss)
    print(run([C.kvm, "--version"]).splitlines()[0])
    print(run([C.swtpm, "--version"]).strip())
    return None


def cmd_check(args):
    ok = True

    def line(good, what, detail=""):
        nonlocal ok
        ok = ok and good
        print("%s  %s%s" % ("ok  " if good else "FAIL", what, (" - " + detail) if detail else ""))

    probs = config_problems()
    line(not probs, "config " + C.path, "; ".join(probs))
    line(os.geteuid() == 0, "running as root")
    line(os.path.exists("/dev/kvm"), "/dev/kvm")
    try:
        res = pve_api("/cluster/status")
        nodes = [r for r in res if r.get("type") == "node"]
        line(True, "Proxmox API (%s)" % ("token" if C.token else "pvesh over SSH"), "%d node(s): %s" % (
            len(nodes), ", ".join("%s=%s" % (n["name"], n.get("ip")) for n in nodes)))
    except Exception as e:
        nodes = []
        line(False, "Proxmox API", str(e))
    for n in nodes:
        try:
            v = pve(n.get("ip"), "pveversion", timeout=20).strip()
            line(True, "root SSH to %s (%s)" % (n["name"], n.get("ip")), v)
        except Exception as e:
            line(False, "root SSH to %s (%s)" % (n["name"], n.get("ip")), str(e)[-200:])
    line(os.path.exists(C.kvm), "Proxmox QEMU bundle " + C.pveq,
         (run([C.kvm, "--version"], check=False).splitlines() or [""])[0] if os.path.exists(C.kvm) else "run fetch-qemu")
    for t in ("lxc-start", "rsync", "zstd", "qemu-img", "dnsmasq", "nft", "ssh"):
        line(bool(shutil.which(t)), "tool " + t)
    gw = host_gateway()
    line(bool(gw), "LAN gateway", gw or "set [network] gateway")
    line(bool(lan_iface()), "LAN interface", lan_iface() or "no default route; set [network] lan_iface")
    print("\nall checks passed" if ok else "\nsome checks failed")
    sys.exit(0 if ok else 1)


def cmd_hash_password(args):
    pw = getpass.getpass("password: ")
    if pw != getpass.getpass("again: "):
        raise UserError("passwords differ")
    print("password_hash = " + make_hash(pw))
    return None


def make_hash(pw, iterations=310000):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), iterations).hex()
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, salt, h)


def check_password(pw):
    if C.password_hash:
        try:
            alg, it, salt, h = C.password_hash.split("$")
            calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it)).hex()
            return alg == "pbkdf2_sha256" and hmac.compare_digest(calc, h)
        except ValueError:
            return False
    return bool(C.password) and hmac.compare_digest(pw.encode(), C.password.encode())


def need_root():
    if os.geteuid() != 0:
        raise UserError("run this as root")


# ================================================================ web console: API
class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def guest_jobs(vmid):
    rep = [j for j in pve_cached("/cluster/replication", 30) if int(j.get("guest", 0)) == vmid]
    bk = []
    for j in pve_cached("/cluster/backup", 60):
        inc = False
        if j.get("all"):
            inc = str(vmid) not in str(j.get("exclude", "")).split(",")
        elif j.get("vmid"):
            inc = str(vmid) in str(j["vmid"]).split(",")
        elif j.get("pool"):
            inc = None
        if inc is not False:
            bk.append(dict(j, _pool_match_unknown=inc is None))
    ha = next((h for h in pve_cached("/cluster/ha/resources", 30) if h.get("sid") in ("vm:%d" % vmid, "ct:%d" % vmid)), None)
    return {"replication": rep, "backup": bk, "ha": ha}


def v_node(n):
    if not re.match(r"^[A-Za-z0-9-]+$", n or ""):
        raise ApiError("bad node")
    return n


def v_tf(q):
    return q.get("tf") if q.get("tf") in ("hour", "day", "week", "month", "year") else "hour"


_hs_cache = [0, None]
_hs_lock = threading.Lock()


def host_status_cached():
    with _hs_lock:
        if time.time() - _hs_cache[0] < 4 and _hs_cache[1]:
            return _hs_cache[1]
    v = cmd_host_status()
    with _hs_lock:
        _hs_cache[:] = [time.time(), v]
    return v


def api_tree(q, post):
    out = {"ok": True, "time": int(time.time()), "host": host_status_cached(), "resources": [], "cluster": None,
           "tag_prefix": C.tag_prefix}
    try:
        out["resources"] = pve_cached("/cluster/resources", 3)
        out["cluster"] = next((r for r in pve_cached("/cluster/status", 15) if r.get("type") == "cluster"), None)
    except Exception as e:
        out["pve_error"] = str(e)
    return out


def api_cluster(q, post):
    return {"ok": True, "replication": pve_cached("/cluster/replication", 30), "backup": pve_cached("/cluster/backup", 60),
            "ha": pve_cached("/cluster/ha/resources", 30)}


def api_node(q, post):
    n = v_node(q.get("node"))
    return {"ok": True, "status": pve_api("/nodes/%s/status" % n),
            "rrd": pve_cached("/nodes/%s/rrddata" % n, 20, {"timeframe": v_tf(q), "cf": "AVERAGE"}),
            "storage": pve_cached("/nodes/%s/storage" % n, 20), "ip": node_ips().get(n)}


def api_guest(q, post):
    n, vmid = v_node(q.get("node")), int(q.get("vmid", 0))
    t = "lxc" if q.get("type") == "lxc" else "qemu"
    cfg = pve_api("/nodes/%s/%s/%d/config" % (n, t, vmid))
    out = {"ok": True, "type": t, "node": n, "node_ip": node_ips().get(n),
           "status": pve_api("/nodes/%s/%s/%d/status/current" % (n, t, vmid)), "config": cfg,
           "rrd": pve_cached("/nodes/%s/%s/%d/rrddata" % (n, t, vmid), 20, {"timeframe": v_tf(q), "cf": "AVERAGE"}),
           "jobs": guest_jobs(vmid), "parked_on": parked_on(cfg.get("tags", "")), "this_host": C.host_name}
    if t == "qemu" and (out["status"] or {}).get("status") == "running" and cfg.get("agent"):
        try:
            out["agent_net"] = (pve_api("/nodes/%s/qemu/%d/agent/network-get-interfaces" % (n, vmid)) or {}).get("result")
        except Exception:
            pass
    if t == "lxc":
        try:
            out["interfaces"] = pve_api("/nodes/%s/lxc/%d/interfaces" % (n, vmid))
        except Exception:
            pass
    try:
        out["snapshots"] = pve_cached("/nodes/%s/%s/%d/snapshot" % (n, t, vmid), 30)
    except Exception:
        pass
    return out


def api_host(q, post):
    return {"ok": True, "status": host_status_cached(), "history": cmd_history()["data"], "jobs": cmd_jobs()["jobs"]}


def api_lguest(q, post):
    vmid = int(q.get("vmid", 0))
    try:
        vm = cmd_vm_status([vmid])
    except UserError as e:
        vm = {"ok": False, "error": str(e)}
    out = {"ok": True, "vm": vm, "history": cmd_history([vmid])["data"],
           "jobs": [j for j in cmd_jobs()["jobs"] if int(j.get("vmid", 0)) == vmid]}
    try:
        out["pvejobs"] = guest_jobs(vmid)
    except Exception:
        pass
    return out


def api_preflight(q, post):
    n, vmid = v_node(q.get("node")), int(q.get("vmid", 0))
    ip = node_ips().get(n) or ""
    if not ip:
        raise ApiError("no IP for node %s" % n)
    return cmd_preflight(n, ip, vmid, "lxc" if q.get("kind") == "lxc" else "qemu")


def api_jobs(q, post):
    tasks = []
    try:
        tasks = pve_cached("/cluster/tasks", 5)[:60]
    except Exception:
        pass
    return {"ok": True, "jobs": cmd_jobs()["jobs"][:80], "tasks": tasks}


def api_job(q, post):
    return cmd_job([q.get("id", "")])


def api_pvetask(q, post):
    n, upid = v_node(q.get("node")), q.get("upid", "")
    e = urllib.parse.quote(upid, safe="")
    return {"ok": True, "status": pve_api("/nodes/%s/tasks/%s/status" % (n, e)),
            "log": pve_api("/nodes/%s/tasks/%s/log" % (n, e), "GET", {"limit": 2000})}


def need_post(post):
    if not post:
        raise ApiError("POST only", 405)


def api_import(q, post):
    need_post(post)
    n, vmid = v_node(q.get("node")), int(q.get("vmid", 0))
    ip = node_ips().get(n) or ""
    if not ip:
        raise ApiError("no IP for node %s" % n)
    p = {"vmid": vmid, "node": n, "node_ip": ip, "kind": "lxc" if q.get("kind") == "lxc" else "qemu",
         "move": "live" if q.get("move") == "live" else "cold", "accept_cpu_risk": q.get("accept_cpu_risk") == "1",
         "net_mode": "nat" if q.get("net_mode") == "nat" else "lan", "guest_ip": q.get("guest_ip", ""),
         "sync_interval": q.get("sync_interval", C.sync_default), "start": q.get("start", "1") == "1",
         "force_stop": q.get("force_stop") == "1",
         "shutdown_timeout": max(30, min(1800, int(q.get("shutdown_timeout") or 300)))}
    cache_clear()
    return start_import(p)


def api_return(q, post):
    need_post(post)
    p = {"vmid": int(q.get("vmid", 0)), "move": "live" if q.get("move") == "live" else "cold",
         "start": q.get("start", "1") == "1", "verify": q.get("verify", "1") == "1",
         "delete_local": q.get("delete_local", "1") == "1", "force_stop": q.get("force_stop") == "1",
         "shutdown_timeout": max(30, min(1800, int(q.get("shutdown_timeout") or 180)))}
    cache_clear()
    return start_return(p)


def api_sync(q, post):
    need_post(post)
    return cmd_sync([int(q.get("vmid", 0))])


def api_setsync(q, post):
    need_post(post)
    return cmd_set_sync([int(q.get("vmid", 0)), re.sub(r"[^a-z0-9]", "", q.get("interval", ""))])


def api_lpower(q, post):
    need_post(post)
    act = q.get("act")
    if act not in ("start", "shutdown", "stop", "reboot"):
        raise ApiError("bad action")
    cache_clear()
    return power(act, [int(q.get("vmid", 0))])


def api_pvepower(q, post):
    need_post(post)
    n, vmid = v_node(q.get("node")), int(q.get("vmid", 0))
    t = "lxc" if q.get("type") == "lxc" else "qemu"
    act = q.get("act")
    if act not in ("start", "shutdown", "stop", "reboot"):
        raise ApiError("bad action")
    if act == "start":
        h = parked_on((pve_api("/nodes/%s/%s/%d/config" % (n, t, vmid)) or {}).get("tags", ""))
        if h:
            raise ApiError("This guest is running on portable host '%s'. Return it to Proxmox first; starting the "
                           "parked copy would split it in two." % h, 409)
    cache_clear()
    return {"ok": True, "upid": pve_api("/nodes/%s/%s/%d/status/%s" % (n, t, vmid, act), "POST")}


API = {"tree": api_tree, "cluster": api_cluster, "node": api_node, "guest": api_guest, "host": api_host,
       "lguest": api_lguest, "preflight": api_preflight, "jobs": api_jobs, "job": api_job, "pvetask": api_pvetask,
       "import": api_import, "return": api_return, "sync": api_sync, "setsync": api_setsync,
       "lpower": api_lpower, "pvepower": api_pvepower}


# ================================================================ web console: HTTP server
SESSIONS = {}
FAILS = {}
_sess_lock = threading.Lock()
COOKIE = "PPSESSION"


def ensure_cert():
    if os.path.exists(C.tls_cert) and os.path.exists(C.tls_key):
        return
    names = ["DNS:" + C.host_name, "DNS:localhost", "IP:127.0.0.1"]
    for i in iface_addrs():
        names += ["IP:" + a.split("/")[0] for a in i["addrs"]]
    run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650", "-sha256",
         "-keyout", C.tls_key, "-out", C.tls_cert, "-subj", "/CN=%s/O=proxmox-portable" % C.host_name,
         "-addext", "subjectAltName=" + ",".join(dict.fromkeys(names))])
    os.chmod(C.tls_key, 0o600)
    print("generated a self-signed certificate %s" % C.tls_cert, flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "proxmox-portable/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    def send(self, code, body, ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self'; frame-ancestors 'none'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def session(self):
        ck = self.headers.get("Cookie", "")
        m = re.search(r"(?:^|;\s*)%s=([A-Za-z0-9_-]+)" % COOKIE, ck)
        if not m:
            return None
        with _sess_lock:
            s = SESSIONS.get(m.group(1))
            if s and s["exp"] > time.time():
                return s
            SESSIONS.pop(m.group(1), None)
        return None

    def do_GET(self):
        self.route(False)

    def do_POST(self):
        self.route(True)

    def route(self, post):
        ip = self.client_address[0]
        try:
            addr = ipaddress.ip_address(ip.replace("::ffff:", "") if ip.startswith("::ffff:") else ip)
        except ValueError:
            addr = None
        if C.allowed and not (addr and (addr.is_loopback or any(addr in n for n in C.allowed))):
            return self.send(403, "forbidden\n", "text/plain")
        u = urllib.parse.urlsplit(self.path)
        if u.path in ("/", "/index.html"):
            return self.send(200, INDEX_HTML, "text/html; charset=utf-8")
        if u.path == "/app.js":
            return self.send(200, APP_JS, "application/javascript; charset=utf-8")
        if u.path == "/app.css":
            return self.send(200, APP_CSS, "text/css; charset=utf-8")
        if u.path != "/api":
            return self.send(404, "not found\n", "text/plain")
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        if post:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1 << 20:
                return self.send(413, {"ok": False, "error": "too large"})
            q.update({k: v[-1] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode(errors="replace")).items()})
            # CSRF: browsers can't send this header cross-site without a CORS preflight, which never succeeds here
            if self.headers.get("X-PP") != "1":
                return self.send(403, {"ok": False, "error": "missing X-PP header"})
        a = q.get("a", "")
        if a == "login":
            return self.login(q, ip, post)
        if a == "logout":
            ck = re.search(r"(?:^|;\s*)%s=([A-Za-z0-9_-]+)" % COOKIE, self.headers.get("Cookie", ""))
            with _sess_lock:
                if ck:
                    SESSIONS.pop(ck.group(1), None)
            return self.send(200, {"ok": True}, headers={"Set-Cookie": "%s=; Path=/; Max-Age=0" % COOKIE})
        s = self.session()
        if not s:
            return self.send(401, {"ok": False, "error": "login required", "login": True})
        if a == "whoami":
            return self.send(200, {"ok": True, "user": s["user"], "host": C.host_name, "version": VERSION})
        fn = API.get(a)
        if not fn:
            return self.send(404, {"ok": False, "error": "unknown action"})
        try:
            return self.send(200, fn(q, post))
        except ApiError as e:
            return self.send(e.code, {"ok": False, "error": str(e)})
        except UserError as e:
            return self.send(400, {"ok": False, "error": str(e)})
        except Exception as e:
            return self.send(500, {"ok": False, "error": str(e)})

    def login(self, q, ip, post):
        if not post:
            return self.send(405, {"ok": False, "error": "POST only"})
        now = time.time()
        with _sess_lock:
            f = [t for t in FAILS.get(ip, []) if now - t < 300]
            FAILS[ip] = f
            if len(f) >= 5:
                return self.send(429, {"ok": False, "error": "too many failed logins; wait a few minutes"})
        user_ok = hmac.compare_digest(q.get("username", "").encode(), C.username.encode())
        pw_ok = check_password(q.get("password", ""))
        if not (user_ok and pw_ok):
            time.sleep(1)
            with _sess_lock:
                FAILS.setdefault(ip, []).append(now)
            return self.send(401, {"ok": False, "error": "wrong username or password", "login": True})
        tok = secrets.token_urlsafe(32)
        with _sess_lock:
            for k in [k for k, v in SESSIONS.items() if v["exp"] < now]:
                SESSIONS.pop(k)
            SESSIONS[tok] = {"user": C.username, "exp": now + C.session_hours * 3600}
        ck = "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s" % (
            COOKIE, tok, int(C.session_hours * 3600), "; Secure" if C.tls else "")
        return self.send(200, {"ok": True, "user": C.username}, headers={"Set-Cookie": ck})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, handler)


def background():
    """Every minute: graph samples and due syncs."""
    while True:
        t = time.time()
        for fn in (sample, sync_due):
            try:
                fn()
            except Exception as e:
                print("%s: %s" % (fn.__name__, e), file=sys.stderr, flush=True)
        time.sleep(max(5, 60 - (time.time() - t)))


def cmd_serve(args):
    need_root()
    probs = config_problems()
    if probs:
        raise UserError("fix the config file %s first:\n  " % C.path + "\n  ".join(probs))
    for d in (C.data, C.vms, C.jobs, C.rrd, C.run):
        os.makedirs(d, exist_ok=True)
    write_helpers()
    marker = C.run + "/booted"
    if not os.path.exists(marker):
        try:
            print("boot: %s" % json.dumps(cmd_boot()), flush=True)
        except Exception as e:
            print("boot: %s" % e, file=sys.stderr, flush=True)
        open(marker, "w").close()
    else:
        try:
            net_up()
        except Exception as e:
            print("net-up: %s" % e, file=sys.stderr, flush=True)
    threading.Thread(target=background, daemon=True).start()
    srv = Server((C.listen, C.port), Handler)
    if C.tls:
        ensure_cert()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(C.tls_cert, C.tls_key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    print("proxmox-portable %s: %s://%s:%d/ (user %s)" % (VERSION, "https" if C.tls else "http", C.listen, C.port, C.username), flush=True)
    srv.serve_forever()


# ================================================================ CLI
def main():
    argv = sys.argv[1:]
    cfg = os.environ.get("PROXMOX_PORTABLE_CONF", DEFAULT_CONF)
    if len(argv) >= 2 and argv[0] in ("--config", "-c"):
        cfg, argv = argv[1], argv[2:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    if cmd == "version":
        print(VERSION)
        return
    try:
        load_config(cfg)
    except UserError as e:
        if cmd != "hash-password":
            print("error: %s" % e, file=sys.stderr)
            sys.exit(2)
    table = {
        "serve": cmd_serve, "setup": cmd_setup, "fetch-qemu": cmd_fetch_qemu, "check": cmd_check,
        "hash-password": cmd_hash_password,
        "status": cmd_host_status, "list": cmd_list, "vm-status": cmd_vm_status, "jobs": cmd_jobs, "job": cmd_job,
        "history": cmd_history,
        "start": lambda a: power("start", a), "shutdown": lambda a: power("shutdown", a),
        "stop": lambda a: power("stop", a), "reboot": lambda a: power("reboot", a),
        "sync": cmd_sync, "set-sync": cmd_set_sync, "boot": cmd_boot,
        "net-up": lambda a: (net_up(), {"ok": True})[1],
        "ifup": cmd_ifup, "ifdown": cmd_ifdown, "_run-job": cmd_run_job,
    }
    if cmd not in table:
        print("unknown command %s (try --help)" % cmd, file=sys.stderr)
        sys.exit(2)
    try:
        r = table[cmd](args)
    except (UserError, RuntimeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
    if r is not None:
        print(json.dumps(r, indent=1, default=str))


# ================================================================ web console: page, style, script
INDEX_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Proxmox Portable</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect x='1' y='3' width='14' height='9' rx='1' fill='%23e57000'/%3E%3Crect x='0' y='12' width='16' height='2' fill='%23555'/%3E%3C/svg%3E">
<link rel="stylesheet" href="app.css?v=''' + VERSION + r'''">
</head>
<body>
<header id="hdr">
  <div class="logo"><span class="mark">PP</span><span class="t1">Proxmox</span><span class="t2">Portable</span><span class="ver" id="ver"></span></div>
  <input id="search" type="search" placeholder="Search guests and nodes ( / )" autocomplete="off">
  <div class="hdr-right">
    <span id="hdr-status" class="muted"></span>
    <span id="user" class="muted"></span>
    <button id="logout" title="Log out">⎋</button>
    <button id="theme" title="Light / dark">◐</button>
    <button id="refresh" title="Refresh now">⟳</button>
  </div>
</header>
<div id="main">
  <aside id="tree-wrap">
    <div class="pane-title">
      <select id="view-sel"><option value="server">Server View</option><option value="type">Type View</option></select>
    </div>
    <div id="tree" tabindex="0"></div>
  </aside>
  <div id="split-v" class="splitter-v"></div>
  <section id="content">
    <div id="crumb"><span id="title"></span><div id="toolbar"></div></div>
    <div id="body">
      <nav id="subnav"></nav>
      <div id="panel"></div>
    </div>
  </section>
</div>
<div id="split-h" class="splitter-h"></div>
<footer id="tasks">
  <div class="tabs"><button class="tab active" data-t="pp">Portable jobs</button><button class="tab" data-t="pve">Proxmox tasks</button></div>
  <div id="task-list"></div>
</footer>
<div id="modal-bg" hidden><div id="modal"><div class="m-title"><span id="m-title"></span><button id="m-close">✕</button></div><div id="m-body"></div><div id="m-foot"></div></div></div>
<div id="login-bg" hidden><form id="login-form" class="login" autocomplete="on">
  <div class="m-title"><span>Proxmox Portable Login</span></div>
  <div class="in">
    <label for="l-user">User name</label><input id="l-user" name="username" autocomplete="username" required>
    <label for="l-pass">Password</label><input id="l-pass" name="password" type="password" autocomplete="current-password" required>
    <div id="l-err" class="lerr"></div>
    <div class="lfoot"><button class="btn primary" id="l-go" type="submit">Login</button></div>
  </div>
</form></div>
<script src="app.js?v=''' + VERSION + r'''"></script>
</body>
</html>
'''

APP_CSS = r'''/* proxmox-portable - styled after the Proxmox VE "crisp" theme */
:root {
  --bg: #f5f5f5; --panel: #ffffff; --head: #ffffff; --line: #cfcfcf; --line2: #e3e3e3;
  --text: #2a2a2a; --muted: #6c6c6c; --sel: #c2ddf2; --hover: #e8f2fa; --accent: #3892d4;
  --accent-d: #2a78b3; --orange: #e57000; --ok: #21a04a; --warn: #e5a000; --err: #d23a3a;
  --btn: #f4f4f4; --btn-b: #c5c5c5; --tbar: #f0f0f0; --code: #f7f7f7; --pp: #7b4fc9;
  --chart1: #3892d4; --chart2: #e57000; --chart3: #21a04a; --grid: #ececec;
}
:root[data-theme="dark"] {
  --bg: #1d1f21; --panel: #26292c; --head: #222527; --line: #3c4044; --line2: #33373a;
  --text: #e2e2e2; --muted: #9aa0a6; --sel: #2c4a63; --hover: #2e3a44; --accent: #4fa3e0;
  --accent-d: #6fb6ea; --btn: #303437; --btn-b: #474c50; --tbar: #2a2d30; --code: #202325;
  --pp: #a982f0; --grid: #33373a;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #1d1f21; --panel: #26292c; --head: #222527; --line: #3c4044; --line2: #33373a;
    --text: #e2e2e2; --muted: #9aa0a6; --sel: #2c4a63; --hover: #2e3a44; --accent: #4fa3e0;
    --accent-d: #6fb6ea; --btn: #303437; --btn-b: #474c50; --tbar: #2a2d30; --code: #202325;
    --pp: #a982f0; --grid: #33373a;
  }
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { background: var(--bg); color: var(--text); font: 13px/1.4 "Open Sans", "Helvetica Neue", Arial, sans-serif;
  display: grid; grid-template-rows: 44px 1fr 5px var(--tasks-h, 190px); overflow: hidden; }
button, input, select, textarea { font: inherit; color: inherit; }
a { color: var(--accent); }
.muted { color: var(--muted); }

/* header */
#hdr { display: flex; align-items: center; gap: 14px; padding: 0 10px; background: var(--head);
  border-bottom: 1px solid var(--line); }
.logo { display: flex; align-items: baseline; gap: 6px; white-space: nowrap; }
.logo .mark { background: var(--orange); color: #fff; font-weight: 700; padding: 2px 6px; border-radius: 2px; letter-spacing: .5px; }
.logo .t1 { font-weight: 700; font-size: 16px; letter-spacing: 1px; text-transform: uppercase; }
.logo .t2 { font-size: 15px; }
.logo .ver { color: var(--muted); font-size: 11px; }
#search { flex: 0 1 340px; padding: 5px 8px; border: 1px solid var(--line); border-radius: 2px; background: var(--panel); }
.hdr-right { margin-left: auto; display: flex; align-items: center; gap: 8px; }
.hdr-right button { border: 1px solid var(--btn-b); background: var(--btn); border-radius: 2px; padding: 3px 9px; cursor: pointer; }

#main { display: grid; grid-template-columns: var(--tree-w, 290px) 5px 1fr; min-height: 0; }
.splitter-v { cursor: col-resize; background: var(--line2); }
.splitter-h { cursor: row-resize; background: var(--line2); }

/* tree */
#tree-wrap { display: flex; flex-direction: column; background: var(--panel); border-right: 1px solid var(--line); min-height: 0; }
.pane-title { padding: 6px; border-bottom: 1px solid var(--line); background: var(--tbar); }
.pane-title select { width: 100%; padding: 3px; border: 1px solid var(--btn-b); background: var(--panel); border-radius: 2px; }
#tree { overflow: auto; flex: 1; padding: 4px 0; outline: none; user-select: none; }
.tn { display: flex; align-items: center; gap: 5px; padding: 2px 6px; white-space: nowrap; cursor: pointer; }
.tn:hover { background: var(--hover); }
.tn.sel { background: var(--sel); }
.tn .tw { width: 12px; text-align: center; color: var(--muted); font-size: 10px; flex: none; }
.tn .ic { width: 16px; height: 16px; flex: none; position: relative; }
.tn .lbl { overflow: hidden; text-overflow: ellipsis; }
.tn .badge { font-size: 10px; padding: 0 4px; border-radius: 2px; background: var(--pp); color: #fff; }
.tn .cpu { margin-left: auto; width: 34px; height: 5px; background: var(--line2); border-radius: 2px; overflow: hidden; flex: none; }
.tn .cpu i { display: block; height: 100%; background: var(--accent); }
.tn.dim .lbl { color: var(--muted); }
.ic svg { width: 16px; height: 16px; display: block; }
.ic .st { position: absolute; right: -3px; bottom: -3px; width: 8px; height: 8px; border-radius: 50%; border: 1px solid var(--panel); }
.st.running, .st.online { background: var(--ok); }
.st.stopped, .st.offline { background: #8a8a8a; }
.st.unknown { background: var(--warn); }
.st.paused { background: var(--warn); }
.st.parked { background: var(--pp); }

/* content */
#content { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--panel); }
#crumb { display: flex; align-items: center; gap: 10px; padding: 6px 10px; border-bottom: 1px solid var(--line);
  background: var(--tbar); min-height: 40px; flex-wrap: wrap; }
#title { font-weight: 600; font-size: 14px; }
#toolbar { margin-left: auto; display: flex; gap: 5px; flex-wrap: wrap; }
.btn { border: 1px solid var(--btn-b); background: var(--btn); border-radius: 2px; padding: 4px 10px; cursor: pointer; white-space: nowrap; }
.btn:hover:not(:disabled) { border-color: var(--accent); }
.btn:disabled { opacity: .45; cursor: default; }
.btn.primary { background: var(--accent); border-color: var(--accent-d); color: #fff; }
.btn.danger { color: var(--err); }
.btn.pp { background: var(--pp); border-color: var(--pp); color: #fff; }
#body { flex: 1; display: grid; grid-template-columns: 180px 1fr; min-height: 0; }
#subnav { border-right: 1px solid var(--line); overflow: auto; padding: 4px 0; background: var(--panel); }
#subnav .sn { padding: 6px 12px; cursor: pointer; display: flex; gap: 8px; align-items: center; }
#subnav .sn:hover { background: var(--hover); }
#subnav .sn.sel { background: var(--sel); }
#subnav .grp { padding: 8px 12px 2px; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; }
#panel { overflow: auto; padding: 12px; min-width: 0; background: var(--bg); }

/* panels */
.box { background: var(--panel); border: 1px solid var(--line); margin-bottom: 12px; }
.box > h3 { margin: 0; padding: 7px 10px; font-size: 13px; font-weight: 600; border-bottom: 1px solid var(--line); background: var(--tbar);
  display: flex; align-items: center; gap: 8px; }
.box > h3 .r { margin-left: auto; font-weight: 400; }
.box > .in { padding: 10px; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 12px; }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 18px; }
.kv > div:nth-child(odd) { color: var(--muted); }
.statrow { display: grid; grid-template-columns: 160px 1fr 140px; align-items: center; gap: 8px; padding: 4px 0; }
.statrow .lab { color: var(--muted); }
.bar { height: 14px; background: var(--line2); border-radius: 2px; overflow: hidden; position: relative; }
.bar i { position: absolute; inset: 0 auto 0 0; background: var(--accent); }
.bar.warn i { background: var(--warn); } .bar.err i { background: var(--err); }
table.grid { width: 100%; border-collapse: collapse; background: var(--panel); }
table.grid th, table.grid td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--line2); vertical-align: top; }
table.grid th { background: var(--tbar); font-weight: 600; border-bottom: 1px solid var(--line); white-space: nowrap; }
table.grid tr.click { cursor: pointer; } table.grid tr.click:hover td { background: var(--hover); }
td.mono, .mono { font-family: "DejaVu Sans Mono", Menlo, Consolas, monospace; font-size: 12px; }
.pill { display: inline-block; padding: 0 6px; border-radius: 9px; font-size: 11px; line-height: 17px; background: var(--line2); }
.pill.ok { background: #d6f0dd; color: #17602e; } .pill.err { background: #f7d9d9; color: #8c1f1f; }
.pill.run { background: #d8e9f7; color: #1d5a8a; } .pill.pp { background: var(--pp); color: #fff; }
:root[data-theme="dark"] .pill.ok { background: #1f4a2c; color: #b9f0c8; }
:root[data-theme="dark"] .pill.err { background: #5a2323; color: #f6c6c6; }
:root[data-theme="dark"] .pill.run { background: #1f3d57; color: #bfe0fa; }
.note { border-left: 3px solid var(--accent); background: var(--panel); padding: 8px 10px; margin: 8px 0; }
.note.warn { border-color: var(--warn); } .note.err { border-color: var(--err); } .note.pp { border-color: var(--pp); }
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); gap: 12px; }
.chart canvas { width: 100%; height: 170px; display: block; }
.chart .legend { display: flex; gap: 12px; font-size: 11px; color: var(--muted); padding: 0 10px 6px; }
.chart .legend i { display: inline-block; width: 10px; height: 3px; vertical-align: middle; margin-right: 4px; }
.form { display: grid; grid-template-columns: 200px 1fr; gap: 8px 12px; align-items: center; max-width: 760px; }
.form input[type=text], .form input[type=number], .form select { padding: 4px 6px; border: 1px solid var(--btn-b); border-radius: 2px; background: var(--panel); max-width: 320px; width: 100%; }
.form .help { grid-column: 2; color: var(--muted); font-size: 12px; margin-top: -4px; }
.steps { margin: 6px 0 0 18px; padding: 0; } .steps li { margin: 3px 0; }
pre.log { background: var(--code); border: 1px solid var(--line); padding: 8px; max-height: 55vh; overflow: auto; margin: 0;
  font: 12px/1.45 "DejaVu Sans Mono", Menlo, Consolas, monospace; white-space: pre-wrap; }
.tfsel { margin-left: auto; }
.tfsel select { padding: 1px 4px; border: 1px solid var(--btn-b); background: var(--panel); border-radius: 2px; }
.empty { color: var(--muted); padding: 20px; text-align: center; }

/* tasks */
#tasks { background: var(--panel); border-top: 1px solid var(--line); display: flex; flex-direction: column; min-height: 0; }
#tasks .tabs { display: flex; background: var(--tbar); border-bottom: 1px solid var(--line); }
#tasks .tab { border: 0; border-right: 1px solid var(--line); background: none; padding: 5px 14px; cursor: pointer; }
#tasks .tab.active { background: var(--panel); font-weight: 600; }
#task-list { overflow: auto; flex: 1; }
#task-list table.grid td, #task-list table.grid th { padding: 3px 8px; }

/* modal */
#modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,.35); display: flex; align-items: center; justify-content: center; z-index: 50; }
#modal-bg[hidden] { display: none; }
#modal { background: var(--panel); border: 1px solid var(--line); width: min(860px, 94vw); max-height: 90vh; display: flex; flex-direction: column; box-shadow: 0 6px 30px rgba(0,0,0,.3); }
.m-title { display: flex; align-items: center; padding: 8px 10px; background: var(--tbar); border-bottom: 1px solid var(--line); font-weight: 600; }
.m-title button { margin-left: auto; border: 0; background: none; cursor: pointer; font-size: 14px; }
#m-body { padding: 12px; overflow: auto; }
#m-foot { padding: 8px 12px; border-top: 1px solid var(--line); display: flex; justify-content: flex-end; gap: 6px; }
#m-foot:empty { display: none; }

@media (max-width: 800px) {
  body { grid-template-rows: auto 1fr 5px 150px; }
  #hdr { flex-wrap: wrap; padding: 6px 16px; }
  #search { flex-basis: 100%; }
  #main { grid-template-columns: 1fr; grid-template-rows: 220px 1fr; }
  #split-v { display: none; }
  #body { grid-template-columns: 1fr; grid-template-rows: auto 1fr; }
  #subnav { display: flex; overflow-x: auto; border-right: 0; border-bottom: 1px solid var(--line); }
  #subnav .grp { display: none; }
  .grid2, .charts { grid-template-columns: 1fr; }
  .form { grid-template-columns: 1fr; } .form .help { grid-column: 1; }
  .statrow { grid-template-columns: 110px 1fr 100px; }
}

/* added for proxmox-portable */
th.kcol { width: 180px; }
.prewrap { white-space: pre-wrap; }
.cin { padding: 4px; border: 1px solid var(--btn-b); background: var(--panel); }
.toast { position: fixed; right: 16px; bottom: 16px; z-index: 80; max-width: 480px; padding: 9px 12px; border: 1px solid var(--line);
  background: var(--panel); border-left: 4px solid var(--ok); box-shadow: 0 3px 12px rgba(0,0,0,.2); }
.toast.err { border-left-color: var(--err); }
#login-bg { position: fixed; inset: 0; background: rgba(0,0,0,.45); display: flex; align-items: center; justify-content: center; z-index: 70; }
#login-bg[hidden] { display: none; }
.login { background: var(--panel); border: 1px solid var(--line); width: min(380px, calc(100vw - 32px)); box-shadow: 0 6px 30px rgba(0,0,0,.35); }
.login .in { padding: 14px; display: grid; grid-template-columns: 90px 1fr; gap: 10px; align-items: center; }
.login input { padding: 5px 7px; border: 1px solid var(--btn-b); border-radius: 2px; background: var(--panel); width: 100%; }
.login .lerr { grid-column: 1 / -1; color: var(--err); min-height: 1em; }
.login .lfoot { grid-column: 1 / -1; display: flex; justify-content: flex-end; }
'''

APP_JS = r'''/* proxmox-portable - web UI (vanilla JS, served by the script itself). */
'use strict';
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
function localGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function localSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
const S = { tree: null, sel: null, sub: null, open: JSON.parse(localGet('pp-open') || '{"dc":1,"host":1}'),
  view: localGet('pp-view') || 'server', detail: null, tf: 'hour', taskTab: 'pp', jobs: null, user: null, loggingIn: false };

// ------------------------------------------------------------- formatting
function fmtBytes(b, d = 1) {
  if (b == null || isNaN(b)) return '-';
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB']; let i = 0; b = +b;
  while (Math.abs(b) >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i ? d : 0) + ' ' + u[i];
}
const fmtRate = b => fmtBytes(b) + '/s';
const pct = (a, b) => b ? (100 * a / b) : 0;
function fmtUp(s) {
  if (!s) return '-'; s = Math.floor(s);
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return (d ? d + ' days ' : '') + String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
}
function fmtTime(t) { if (!t) return '-'; const d = new Date(t * 1000); return d.toLocaleString(undefined, { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }); }
function ago(t) { if (!t) return 'never'; const s = Date.now() / 1000 - t; if (s < 90) return Math.round(s) + ' s ago'; if (s < 5400) return Math.round(s / 60) + ' min ago'; if (s < 172800) return (s / 3600).toFixed(1) + ' h ago'; return Math.round(s / 86400) + ' days ago'; }
function pill(status) {
  const c = { running: 'run', online: 'ok', ok: 'ok', OK: 'ok', stopped: '', failed: 'err', offline: 'err', queued: '', 'on-host': 'pp' }[status] ?? '';
  return `<span class="pill ${c}">${esc(status)}</span>`;
}
function bar(p, label) { p = Math.max(0, Math.min(100, p || 0)); return `<div class="bar ${p > 90 ? 'err' : p > 75 ? 'warn' : ''}" title="${esc(label || p.toFixed(1) + '%')}"><i style="width:${p}%"></i></div>`; }
function statrow(label, p, text) { return `<div class="statrow"><div class="lab">${esc(label)}</div>${bar(p)}<div>${text}</div></div>`; }
const HN = () => (S.tree && S.tree.host && S.tree.host.name) || 'this host';

// ------------------------------------------------------------- icons
const ICON = {
  dc: '<svg viewBox="0 0 16 16"><rect x="1" y="2" width="14" height="4" rx="1" fill="#7a8a99"/><rect x="1" y="7" width="14" height="4" rx="1" fill="#7a8a99"/><rect x="1" y="12" width="14" height="3" rx="1" fill="#7a8a99"/><circle cx="3.5" cy="4" r=".9" fill="#fff"/><circle cx="3.5" cy="9" r=".9" fill="#fff"/></svg>',
  node: '<svg viewBox="0 0 16 16"><rect x="2" y="1" width="12" height="14" rx="1" fill="#5d6d7e"/><rect x="4" y="3" width="8" height="1.6" fill="#cfd8df"/><rect x="4" y="6" width="8" height="1.6" fill="#cfd8df"/><circle cx="8" cy="11.5" r="1.4" fill="#cfd8df"/></svg>',
  qemu: '<svg viewBox="0 0 16 16"><rect x="1" y="2" width="14" height="10" rx="1" fill="#3892d4"/><rect x="2.5" y="3.5" width="11" height="7" fill="#e9f3fb"/><rect x="5" y="12" width="6" height="1.5" fill="#3892d4"/><rect x="3.5" y="13.5" width="9" height="1.2" fill="#3892d4"/></svg>',
  lxc: '<svg viewBox="0 0 16 16"><path d="M8 1 15 4.5v7L8 15 1 11.5v-7z" fill="#5aa469"/><path d="M8 1 15 4.5 8 8 1 4.5z" fill="#7cc58b"/><path d="M8 8v7" stroke="#3f7a4b" stroke-width="1"/></svg>',
  host: '<svg viewBox="0 0 16 16"><rect x="2" y="3" width="12" height="8" rx="1" fill="#7b4fc9"/><rect x="3" y="4" width="10" height="6" fill="#ece4fb"/><path d="M0 12h16l-1.5 2h-13z" fill="#5c3a99"/></svg>',
  storage: '<svg viewBox="0 0 16 16"><ellipse cx="8" cy="3.5" rx="6" ry="2.2" fill="#8a7a5a"/><path d="M2 3.5v9c0 1.2 2.7 2.2 6 2.2s6-1 6-2.2v-9c0 1.2-2.7 2.2-6 2.2S2 4.7 2 3.5z" fill="#a8956b"/></svg>',
};
function icon(kind, st) { return `<span class="ic">${ICON[kind] || ''}${st ? `<span class="st ${esc(st)}"></span>` : ''}</span>`; }

// ------------------------------------------------------------- API + login
async function api(a, params = {}, post = false) {
  const qs = new URLSearchParams({ a, ...params });
  let r;
  try {
    r = post ? await fetch('api', { method: 'POST', body: qs, headers: { 'X-PP': '1' }, credentials: 'same-origin' })
      : await fetch('api?' + qs, { credentials: 'same-origin' });
  } catch (e) { return { ok: false, error: 'network: ' + e.message }; }
  let j;
  try { j = await r.json(); } catch (e) { return { ok: false, error: 'HTTP ' + r.status }; }
  if (r.status === 401 && j.login && a !== 'login') showLogin();
  return j;
}
function showLogin(msg) {
  if (S.loggingIn && !msg) return;
  S.loggingIn = true;
  $('#login-bg').hidden = false;
  $('#l-err').textContent = msg || '';
  setTimeout(() => ($('#l-user').value ? $('#l-pass') : $('#l-user')).focus(), 30);
}
$('#login-form').addEventListener('submit', async e => {
  e.preventDefault();
  $('#l-go').disabled = true;
  const r = await api('login', { username: $('#l-user').value, password: $('#l-pass').value }, true);
  $('#l-go').disabled = false;
  if (!r.ok) { showLogin(r.error); return; }
  $('#l-pass').value = ''; $('#login-bg').hidden = true; S.loggingIn = false; S.user = r.user;
  $('#user').textContent = r.user; refresh(); refreshJobs();
});
$('#logout').onclick = async () => { await api('logout', {}, true); S.tree = null; $('#tree').innerHTML = ''; $('#panel').innerHTML = ''; showLogin(); };

// ------------------------------------------------------------- tree model
function parkedOn(tags) {
  const pre = (S.tree && S.tree.tag_prefix) || 'portable-';
  const t = String(tags || '').split(/[;, ]+/).find(x => x.startsWith(pre) && x.length > pre.length);
  return t ? t.slice(pre.length) : null;
}
const activeGuests = () => ((S.tree.host || {}).guests || []).filter(g => !['returned', 'failed-import'].includes(g.pp_state));
function buildTree() {
  const T = S.tree; if (!T) return [];
  const res = T.resources || [];
  const nodes = res.filter(r => r.type === 'node').sort((a, b) => a.node.localeCompare(b.node));
  const guests = res.filter(r => r.type === 'qemu' || r.type === 'lxc').sort((a, b) => a.vmid - b.vmid);
  const g2n = g => ({ id: `${g.type}/${g.vmid}`, kind: g.type, label: `${g.vmid} (${g.name || ''})`, st: parkedOn(g.tags) ? 'parked' : g.status,
    badge: parkedOn(g.tags) ? '→ ' + parkedOn(g.tags) : (g.template ? 'tpl' : ''), dim: !!g.template || !!parkedOn(g.tags), cpu: g.status === 'running' ? g.cpu : null, data: g });
  const dc = { id: 'dc', kind: 'dc', label: 'Datacenter' + (T.cluster ? ` (${T.cluster.name})` : ''), children: [] };
  if (S.view === 'type') {
    dc.children.push({ id: 'grp/node', kind: 'node', label: 'Nodes', children: nodes.map(n => ({ id: 'node/' + n.node, kind: 'node', label: n.node, st: n.status, cpu: n.cpu, data: n })) });
    dc.children.push({ id: 'grp/qemu', kind: 'qemu', label: 'Virtual Machines', children: guests.filter(g => g.type === 'qemu').map(g2n) });
    dc.children.push({ id: 'grp/lxc', kind: 'lxc', label: 'Containers', children: guests.filter(g => g.type === 'lxc').map(g2n) });
  } else {
    for (const n of nodes) {
      dc.children.push({ id: 'node/' + n.node, kind: 'node', label: n.node, st: n.status, cpu: n.status === 'online' ? n.cpu : null, data: n,
        children: guests.filter(g => g.node === n.node).map(g2n) });
    }
  }
  const h = T.host || {};
  const hn = { id: 'host', kind: 'host', label: `${h.name || 'this host'} (portable)`, st: 'online', cpu: h.cpu, data: h,
    children: activeGuests().map(g => ({ id: `hvm/${g.vmid}`, kind: g.kind === 'lxc' ? 'lxc' : 'qemu',
      label: `${g.vmid} (${g.name})`, st: g.status === 'running' ? 'running' : g.status === 'stopped' ? 'stopped' : 'unknown',
      badge: g.pp_state !== 'on-host' ? g.pp_state : '', data: g })) };
  return [dc, hn];
}
function flatFind(id, list = buildTree()) {
  for (const n of list) { if (n.id === id) return n; if (n.children) { const f = flatFind(id, n.children); if (f) return f; } }
  return null;
}
function renderTree() {
  const q = $('#search').value.trim().toLowerCase();
  const html = [];
  const walk = (n, depth) => {
    const kids = n.children || [];
    const match = !q || n.label.toLowerCase().includes(q);
    const save = html.length;
    const open = q ? true : !!S.open[n.id];
    if (!match && !kids.length) return false;
    html.push(null);
    let any = false;
    if (open) for (const k of kids) any = walk(k, depth + 1) || any;
    if (!match && !any) { html.length = save; return false; }
    html[save] = `<div class="tn ${S.sel === n.id ? 'sel' : ''} ${n.dim ? 'dim' : ''}" data-id="${esc(n.id)}" style="padding-left:${6 + depth * 16}px">
      <span class="tw" data-tw="1">${kids.length ? (open ? '▾' : '▸') : ''}</span>${icon(n.kind, n.st)}
      <span class="lbl">${esc(n.label)}</span>${n.badge ? `<span class="badge">${esc(n.badge)}</span>` : ''}
      ${n.cpu != null ? `<span class="cpu" title="CPU ${(n.cpu * 100).toFixed(0)}%"><i style="width:${Math.min(100, n.cpu * 100)}%"></i></span>` : ''}</div>`;
    return true;
  };
  for (const r of buildTree()) walk(r, 0);
  $('#tree').innerHTML = html.join('');
}
$('#tree').addEventListener('click', e => {
  const tn = e.target.closest('.tn'); if (!tn) return;
  const id = tn.dataset.id;
  if (e.target.dataset.tw) { S.open[id] = !S.open[id]; localSet('pp-open', JSON.stringify(S.open)); renderTree(); return; }
  select(id);
});
$('#tree').addEventListener('dblclick', e => { const tn = e.target.closest('.tn'); if (!tn) return; S.open[tn.dataset.id] = !S.open[tn.dataset.id]; renderTree(); });
$('#search').addEventListener('input', renderTree);
document.addEventListener('keydown', e => { if (e.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) { e.preventDefault(); $('#search').focus(); } if (e.key === 'Escape') closeModal(); });
$('#view-sel').value = S.view;
$('#view-sel').addEventListener('change', e => { S.view = e.target.value; localSet('pp-view', S.view); renderTree(); });

// ------------------------------------------------------------- selection & views
function select(id, sub) {
  if (S.sel !== id) { S.sub = sub || null; S.detail = null; }
  if (sub) S.sub = sub;
  S.sel = id; location.hash = id + (S.sub ? '|' + S.sub : '');
  renderTree(); renderView(true);
}
function kindOf(id) { return id.split('/')[0]; }

const VIEWS = {
  dc: { title: n => 'Datacenter', nav: [['summary', 'Summary'], ['portable', 'Portable guests'], ['replication', 'Replication'], ['backup', 'Backup'], ['guide', 'How it works']] },
  node: { title: n => `Node '${n.label}'`, nav: [['summary', 'Summary'], ['guests', 'Guests'], ['storage', 'Storage']] },
  qemu: { title: n => `Virtual Machine ${n.data.vmid} (${n.data.name}) on node '${n.data.node}'`, nav: [['summary', 'Summary'], ['hardware', 'Hardware'], ['options', 'Options'], ['replication', 'Replication'], ['backup', 'Backup'], ['snapshots', 'Snapshots'], ['portable', 'Move here']] },
  lxc: { title: n => `Container ${n.data.vmid} (${n.data.name}) on node '${n.data.node}'`, nav: [['summary', 'Summary'], ['resources', 'Resources'], ['options', 'Options'], ['replication', 'Replication'], ['backup', 'Backup'], ['snapshots', 'Snapshots'], ['portable', 'Move here']] },
  host: { title: n => `Portable host '${HN()}'`, nav: [['summary', 'Summary'], ['guests', 'Guests'], ['network', 'Network'], ['jobs', 'Jobs'], ['guide', 'How it works']] },
  hvm: { title: n => `${n.data.kind === 'lxc' ? 'Container' : 'Virtual Machine'} ${n.data.vmid} (${n.data.name}) on '${HN()}'`, nav: [['summary', 'Summary'], ['hardware', 'Hardware'], ['sync', 'Sync & Backup'], ['return', 'Return to Proxmox'], ['jobs', 'Jobs']] },
  grp: { title: n => n.label, nav: [['summary', 'Summary']] },
};

async function renderView(full) {
  const id = S.sel; const node = id && flatFind(id);
  if (!node) { $('#title').textContent = id ? 'Not found: ' + id : ''; $('#panel').innerHTML = '<div class="empty">Select something in the tree.</div>'; $('#subnav').innerHTML = ''; $('#toolbar').innerHTML = ''; return; }
  const k = kindOf(id); const V = VIEWS[k];
  if (!S.sub || !V.nav.some(x => x[0] === S.sub)) S.sub = V.nav[0][0];
  $('#title').textContent = V.title(node);
  $('#subnav').innerHTML = V.nav.map(([key, lbl]) => `<div class="sn ${key === S.sub ? 'sel' : ''}" data-sub="${key}">${esc(lbl)}</div>`).join('');
  renderToolbar(k, node);
  const want = id + '|' + S.sub;
  if (full) $('#panel').innerHTML = '<div class="empty">Loading…</div>';
  try {
    const html = await PANELS[k](node, S.sub);
    if (S.sel + '|' + S.sub !== want) return;
    if (html != null) {
      const keepScroll = $('#panel').scrollTop;
      const focusId = document.activeElement && document.activeElement.id;
      if (!full && $('#panel').querySelector('.form') && S.sub !== 'summary') { drawCharts(); return; } // don't clobber forms on refresh
      $('#panel').innerHTML = html; $('#panel').scrollTop = keepScroll;
      if (focusId && $('#' + focusId)) $('#' + focusId).focus();
      bindPanel(k, node);
      drawCharts();
    }
  } catch (e) { $('#panel').innerHTML = `<div class="note err">${esc(e.message || e)}</div>`; }
}
$('#subnav').addEventListener('click', e => { const sn = e.target.closest('.sn'); if (sn) { S.sub = sn.dataset.sub; location.hash = S.sel + '|' + S.sub; renderView(true); } });

// ------------------------------------------------------------- toolbar
function renderToolbar(k, n) {
  let h = '';
  if (k === 'qemu' || k === 'lxc') {
    const g = n.data; const run = g.status === 'running'; const parked = parkedOn(g.tags);
    h += `<button class="btn" data-pv="start" ${run || parked || g.template ? 'disabled' : ''}>▶ Start</button>
      <button class="btn" data-pv="shutdown" ${!run ? 'disabled' : ''}>⏻ Shutdown</button>
      <button class="btn danger" data-pv="stop" ${!run ? 'disabled' : ''}>■ Stop</button>
      <button class="btn" data-pv="reboot" ${!run ? 'disabled' : ''}>↻ Reboot</button>`;
    h += parked ? (parked === HN() ? `<button class="btn pp" data-goto="hvm/${g.vmid}">Open on ${esc(parked)}</button>` : `<button class="btn pp" disabled>On ${esc(parked)}</button>`)
      : `<button class="btn pp" data-tosub="portable" ${g.template ? 'disabled' : ''}>⇣ Move to ${esc(HN())}</button>`;
  } else if (k === 'hvm') {
    const g = n.data; const run = g.status === 'running'; const ok = g.pp_state === 'on-host';
    h += `<button class="btn" data-lp="start" ${run || !ok ? 'disabled' : ''}>▶ Start</button>
      <button class="btn" data-lp="shutdown" ${!run ? 'disabled' : ''}>⏻ Shutdown</button>
      <button class="btn danger" data-lp="stop" ${!run ? 'disabled' : ''}>■ Stop</button>
      <button class="btn" data-lp="reboot" ${!run ? 'disabled' : ''}>↻ Reboot</button>
      <button class="btn" data-sync="1" ${!ok ? 'disabled' : ''}>⇅ Sync now</button>
      <button class="btn primary" data-tosub="return" ${!ok ? 'disabled' : ''}>⤺ Return to Proxmox</button>`;
  }
  $('#toolbar').innerHTML = h;
}
$('#toolbar').addEventListener('click', async e => {
  const b = e.target.closest('button'); if (!b || b.disabled) return;
  const n = flatFind(S.sel); if (!n) return;
  if (b.dataset.tosub) { S.sub = b.dataset.tosub; renderView(true); return; }
  if (b.dataset.goto) { select(b.dataset.goto); return; }
  if (b.dataset.pv) {
    const act = b.dataset.pv, g = n.data;
    if (act !== 'start' && !await confirmTyped(`${act} ${g.type === 'lxc' ? 'CT' : 'VM'} ${g.vmid} (${g.name}) on ${g.node}?`, act === 'stop' ? String(g.vmid) : null)) return;
    const r = await api('pvepower', { node: g.node, vmid: g.vmid, type: g.type, act }, true);
    toast(r.ok ? `${act} requested (${r.upid || ''})` : r.error, !r.ok); refresh();
  }
  if (b.dataset.lp) {
    const act = b.dataset.lp, g = n.data;
    if (act !== 'start' && !await confirmTyped(`${act} ${g.kind === 'lxc' ? 'CT' : 'VM'} ${g.vmid} (${g.name}) on ${HN()}?`, act === 'stop' ? String(g.vmid) : null)) return;
    b.disabled = true;
    const r = await api('lpower', { vmid: g.vmid, act }, true);
    toast(r.ok ? `${act}: ${r.status}` : r.error, !r.ok); refresh();
  }
  if (b.dataset.sync) {
    const g = n.data; const r = await api('sync', { vmid: g.vmid }, true);
    toast(r.ok ? 'Sync job started: ' + r.job : r.error, !r.ok); refreshJobs();
  }
});

// ------------------------------------------------------------- charts
const CHARTS = [];
function chart(title, series, opts = {}) {
  const id = 'c' + CHARTS.length + '-' + Math.random().toString(36).slice(2, 7);
  CHARTS.push({ id, series, opts });
  return `<div class="box chart"><h3>${esc(title)}</h3><canvas id="${id}"></canvas><div class="legend">${series.map(s => `<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join('')}</div></div>`;
}
function drawCharts() {
  const cs = getComputedStyle(document.documentElement);
  const grid = cs.getPropertyValue('--grid').trim(), muted = cs.getPropertyValue('--muted').trim();
  for (const c of CHARTS.splice(0)) {
    const cv = document.getElementById(c.id); if (!cv) continue;
    const dpr = window.devicePixelRatio || 1; const W = cv.clientWidth, H = cv.clientHeight;
    cv.width = W * dpr; cv.height = H * dpr; const x = cv.getContext('2d'); x.scale(dpr, dpr);
    const L = 62, R = 10, T = 10, B = 22;
    const all = c.series.flatMap(s => s.pts).filter(p => p[1] != null && !isNaN(p[1]));
    if (!all.length) { x.fillStyle = muted; x.font = '12px sans-serif'; x.fillText('no data yet', W / 2 - 30, H / 2); continue; }
    const t0 = Math.min(...all.map(p => p[0])), t1 = Math.max(...all.map(p => p[0]));
    let vmax = c.opts.max ?? Math.max(...all.map(p => p[1])); if (!(vmax > 0)) vmax = 1;
    if (c.opts.unit === 'pct') vmax = Math.max(vmax, 0.05);
    vmax *= c.opts.max ? 1 : 1.1;
    const fx = t => L + (W - L - R) * (t1 === t0 ? 1 : (t - t0) / (t1 - t0)), fy = v => T + (H - T - B) * (1 - v / vmax);
    x.strokeStyle = grid; x.fillStyle = muted; x.font = '11px sans-serif'; x.lineWidth = 1;
    const fmt = v => c.opts.unit === 'pct' ? (v * 100).toFixed(0) + '%' : c.opts.unit === 'rate' ? fmtRate(v) : c.opts.unit === 'bytes' ? fmtBytes(v, 1) : v.toFixed(2);
    for (let i = 0; i <= 4; i++) { const v = vmax * i / 4, y = fy(v); x.beginPath(); x.moveTo(L, y); x.lineTo(W - R, y); x.stroke(); x.fillText(fmt(v), 4, y + 4); }
    for (let i = 0; i <= 4; i++) { const t = t0 + (t1 - t0) * i / 4; const d = new Date(t * 1000);
      const s = (t1 - t0) > 2 * 86400 ? d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
      x.fillText(s, Math.min(W - R - 34, fx(t) - 14), H - 6); }
    for (const s of c.series) {
      const pts = s.pts.filter(p => p[1] != null && !isNaN(p[1])); if (!pts.length) continue;
      x.strokeStyle = s.color; x.lineWidth = 1.5; x.beginPath();
      pts.forEach((p, i) => i ? x.lineTo(fx(p[0]), fy(p[1])) : x.moveTo(fx(p[0]), fy(p[1]))); x.stroke();
      if (s.fill) { x.lineTo(fx(pts[pts.length - 1][0]), fy(0)); x.lineTo(fx(pts[0][0]), fy(0)); x.closePath(); x.globalAlpha = .15; x.fillStyle = s.color; x.fill(); x.globalAlpha = 1; }
    }
  }
}
window.addEventListener('resize', () => renderView(false));
const C1 = '#3892d4', C2 = '#e57000';
function rrdPts(rrd, f, mul = 1) { return (rrd || []).map(r => [r.time, r[f] == null ? null : r[f] * mul]); }
function rrdCharts(rrd, kind) {
  if (kind === 'node') return chart('CPU usage', [{ label: 'CPU', color: C1, fill: 1, pts: rrdPts(rrd, 'cpu') }, { label: 'IO delay', color: C2, pts: rrdPts(rrd, 'iowait') }], { unit: 'pct' })
    + chart('Server load', [{ label: 'Load average', color: C1, fill: 1, pts: rrdPts(rrd, 'loadavg') }], { unit: 'num' })
    + chart('Memory usage', [{ label: 'Total', color: C2, pts: rrdPts(rrd, 'memtotal') }, { label: 'RAM used', color: C1, fill: 1, pts: rrdPts(rrd, 'memused') }], { unit: 'bytes' })
    + chart('Network traffic', [{ label: 'In', color: C1, fill: 1, pts: rrdPts(rrd, 'netin') }, { label: 'Out', color: C2, pts: rrdPts(rrd, 'netout') }], { unit: 'rate' });
  return chart('CPU usage', [{ label: 'CPU', color: C1, fill: 1, pts: rrdPts(rrd, 'cpu') }], { unit: 'pct' })
    + chart('Memory usage', [{ label: 'Total', color: C2, pts: rrdPts(rrd, 'maxmem') }, { label: 'RAM used', color: C1, fill: 1, pts: rrdPts(rrd, 'mem') }], { unit: 'bytes' })
    + chart('Network traffic', [{ label: 'In', color: C1, fill: 1, pts: rrdPts(rrd, 'netin') }, { label: 'Out', color: C2, pts: rrdPts(rrd, 'netout') }], { unit: 'rate' })
    + chart('Disk IO', [{ label: 'Read', color: C1, fill: 1, pts: rrdPts(rrd, 'diskread') }, { label: 'Write', color: C2, pts: rrdPts(rrd, 'diskwrite') }], { unit: 'rate' });
}
function histCharts(h, host) {
  const p = f => (h || []).map(s => [s.t, s[f]]);
  return chart('CPU usage', [{ label: 'CPU', color: C1, fill: 1, pts: p('cpu') }], { unit: 'pct' })
    + (host ? chart('Server load', [{ label: 'Load average', color: C1, fill: 1, pts: p('load') }], { unit: 'num' }) : '')
    + chart('Memory usage', [{ label: 'Total', color: C2, pts: p('memtotal') }, { label: 'RAM used', color: C1, fill: 1, pts: p('mem') }], { unit: 'bytes' })
    + chart('Network traffic', [{ label: 'In', color: C1, fill: 1, pts: p('netin') }, { label: 'Out', color: C2, pts: p('netout') }], { unit: 'rate' })
    + chart('Disk IO', [{ label: 'Read', color: C1, fill: 1, pts: p('diskread') }, { label: 'Write', color: C2, pts: p('diskwrite') }], { unit: 'rate' });
}
function tfSel() { return `<span class="tfsel"><select id="tf">${['hour', 'day', 'week', 'month', 'year'].map(t => `<option ${t === S.tf ? 'selected' : ''} value="${t}">${t[0].toUpperCase() + t.slice(1)} (average)</option>`).join('')}</select></span>`; }

// ------------------------------------------------------------- panels
const PANELS = {};
function guestsByNode(n) { return (S.tree.resources || []).filter(r => (r.type === 'qemu' || r.type === 'lxc') && r.node === n); }
function guestTable(list, local) {
  if (!list.length) return '<div class="empty">No guests.</div>';
  return `<table class="grid"><tr><th>ID</th><th>Name</th><th>Type</th><th>Status</th><th>CPU</th><th>Memory</th><th>Disk</th><th>Uptime</th><th>Tags</th></tr>${list.map(g => {
    const id = local ? `hvm/${g.vmid}` : `${g.type}/${g.vmid}`;
    return `<tr class="click" data-goto="${esc(id)}"><td>${g.vmid}</td><td>${esc(g.name)}</td><td>${esc(g.type || 'qemu')}</td><td>${pill(parkedOn(g.tags) ? 'on ' + parkedOn(g.tags) : g.status)}</td>
    <td>${g.cpu != null && g.status === 'running' ? (g.cpu * 100).toFixed(1) + '%' : '-'}</td><td>${g.maxmem ? fmtBytes(g.mem) + ' / ' + fmtBytes(g.maxmem) : g.memory ? g.memory + ' MiB' : '-'}</td>
    <td>${g.maxdisk ? fmtBytes(g.maxdisk) : '-'}</td><td>${g.uptime ? fmtUp(g.uptime) : '-'}</td><td>${esc((g.tags || '').replace(/;/g, ', '))}</td></tr>`; }).join('')}</table>`;
}
function pveErr() { return S.tree && S.tree.pve_error ? `<div class="note err"><b>Proxmox cluster not reachable:</b> ${esc(S.tree.pve_error)}</div>` : ''; }

PANELS.dc = async (n, sub) => {
  const T = S.tree; const res = T.resources || [];
  const nodes = res.filter(r => r.type === 'node'); const guests = res.filter(r => r.type === 'qemu' || r.type === 'lxc');
  if (sub === 'summary') {
    const on = nodes.filter(x => x.status === 'online');
    const cpu = on.reduce((a, x) => a + x.cpu * x.maxcpu, 0), maxcpu = on.reduce((a, x) => a + x.maxcpu, 0);
    const mem = on.reduce((a, x) => a + x.mem, 0), maxmem = on.reduce((a, x) => a + x.maxmem, 0);
    const parked = guests.filter(g => parkedOn(g.tags));
    return pveErr() + `<div class="grid2"><div class="box"><h3>Health</h3><div class="in kv">
      <div>Cluster</div><div>${esc(T.cluster ? T.cluster.name : '-')} ${T.cluster ? (T.cluster.quorate ? pill('quorate') : '<span class="pill err">NOT quorate</span>') : ''}</div>
      <div>Nodes</div><div>${on.length} online / ${nodes.length - on.length} offline</div>
      <div>Guests</div><div>${guests.filter(g => g.type === 'qemu').length} VMs (${guests.filter(g => g.type === 'qemu' && g.status === 'running').length} running), ${guests.filter(g => g.type === 'lxc').length} containers (${guests.filter(g => g.type === 'lxc' && g.status === 'running').length} running)</div>
      <div>Portable host</div><div><a href="#" data-goto="host">${esc(HN())}</a>: ${activeGuests().filter(g => g.pp_state === 'on-host').length} guest(s) here</div>
      <div>Parked in the cluster</div><div>${parked.length} (${[...new Set(parked.map(g => parkedOn(g.tags)))].map(esc).join(', ') || 'none'})</div>
      </div></div><div class="box"><h3>Resources (online nodes)</h3><div class="in">
      ${statrow('CPU', pct(cpu, maxcpu), maxcpu ? `${(100 * cpu / maxcpu).toFixed(1)}% of ${maxcpu} CPUs` : '-')}
      ${statrow('Memory', pct(mem, maxmem), `${fmtBytes(mem)} of ${fmtBytes(maxmem)}`)}</div></div></div>
      <div class="box"><h3>Nodes</h3><table class="grid"><tr><th>Name</th><th>Status</th><th>CPU</th><th>Memory</th><th>Uptime</th><th>Guests</th></tr>
      ${nodes.sort((a, b) => a.node.localeCompare(b.node)).map(x => `<tr class="click" data-goto="node/${esc(x.node)}"><td>${esc(x.node)}</td><td>${pill(x.status)}</td><td>${x.status === 'online' ? (x.cpu * 100).toFixed(1) + '% of ' + x.maxcpu : '-'}</td><td>${x.maxmem ? fmtBytes(x.mem) + ' / ' + fmtBytes(x.maxmem) : '-'}</td><td>${fmtUp(x.uptime)}</td><td>${guestsByNode(x.node).length}</td></tr>`).join('')}</table></div>`;
  }
  if (sub === 'portable') {
    const rows = activeGuests();
    const parked = guests.filter(g => parkedOn(g.tags));
    return `<div class="note pp">Guests that left the cluster to run on a portable host. Their Proxmox copy stays parked (stopped, <span class="mono">onboot=0</span>, tag <span class="mono">${esc(T.tag_prefix)}&lt;host&gt;</span>) on the original node and is kept current by the host's sync, so Proxmox replication and scheduled backups keep working on it.</div>
      <div class="box"><h3>Running on ${esc(HN())}</h3>${rows.length ? `<table class="grid"><tr><th>ID</th><th>Name</th><th>State</th><th>Status</th><th>From node</th><th>Sync</th><th>Last sync</th><th>Network</th></tr>${rows.map(g => `<tr class="click" data-goto="hvm/${g.vmid}"><td>${g.vmid}</td><td>${esc(g.name)}</td><td>${pill(g.pp_state)}</td><td>${pill(g.status)}</td><td>${esc(g.origin_node)}</td><td>${esc(g.sync_interval)}</td><td>${g.last_sync ? ago(g.last_sync.time) + ' (' + fmtBytes(g.last_sync.bytes) + ')' : 'never'}</td><td>${esc(g.net_mode)}${g.guest_ip ? ' ' + esc(g.guest_ip) : ''}</td></tr>`).join('')}</table>` : '<div class="empty">No guest is on this host.</div>'}</div>
      <div class="box"><h3>Parked Proxmox copies (all portable hosts)</h3>${guestTable(parked)}</div>`;
  }
  if (sub === 'replication' || sub === 'backup') {
    const r = await api('cluster');
    if (!r.ok) return `<div class="note err">${esc(r.error)}</div>`;
    const gOf = id => guests.find(x => x.vmid == id);
    const name = id => { const g = gOf(id); return g ? `${id} (${esc(g.name)})` : id; };
    const ppTag = id => parkedOn((gOf(id) || {}).tags) ? ' <span class="pill pp">portable</span>' : '';
    if (sub === 'replication') return `<div class="box"><h3>Replication jobs</h3><table class="grid"><tr><th>Guest</th><th>Job</th><th>Source</th><th>Target</th><th>Schedule</th><th>Rate</th><th>Enabled</th></tr>${(r.replication || []).sort((a, b) => a.guest - b.guest).map(x => `<tr class="click" data-goto="${gOf(x.guest) ? gOf(x.guest).type + '/' + x.guest : 'dc'}"><td>${name(x.guest)}${ppTag(x.guest)}</td><td class="mono">${esc(x.id)}</td><td>${esc(x.source)}</td><td>${esc(x.target)}</td><td class="mono">${esc(x.schedule || '*/15')}</td><td>${x.rate ? x.rate + ' MB/s' : '-'}</td><td>${x.disable ? 'no' : 'yes'}</td></tr>`).join('')}</table></div>`;
    return `<div class="box"><h3>Backup jobs</h3><table class="grid"><tr><th>Job</th><th>Schedule</th><th>Storage</th><th>Mode</th><th>Retention</th><th>Enabled</th><th>Guests</th></tr>${(r.backup || []).map(x => `<tr><td class="mono">${esc(x.id)}</td><td class="mono">${esc(x.schedule)}</td><td>${esc(x.storage)}</td><td>${esc(x.mode)}</td><td class="mono">${esc(x['prune-backups'] || '')}</td><td>${x.enabled == 0 ? 'no' : 'yes'}</td><td>${x.all ? 'all' + (x.exclude ? ' except ' + esc(x.exclude) : '') : x.pool ? 'pool ' + esc(x.pool) : String(x.vmid || '').split(',').map(v => name(v) + ppTag(v)).join(', ')}</td></tr>`).join('')}</table></div>`;
  }
  if (sub === 'guide') return guide();
};

PANELS.grp = async (n) => guestTable((n.children || []).map(c => c.data).filter(x => x && x.vmid));

PANELS.node = async (n, sub) => {
  const name = n.data.node;
  if (sub === 'guests') return `<div class="box"><h3>Guests on ${esc(name)}</h3>${guestTable(guestsByNode(name).sort((a, b) => a.vmid - b.vmid))}</div>`;
  const r = await api('node', { node: name, tf: S.tf });
  if (!r.ok) return `<div class="note err">${esc(r.error)}</div>`;
  const s = r.status;
  if (sub === 'storage') return `<div class="box"><h3>Storage on ${esc(name)}</h3><table class="grid"><tr><th>Name</th><th>Type</th><th>Content</th><th>Shared</th><th>Usage</th><th></th></tr>${(r.storage || []).sort((a, b) => a.storage.localeCompare(b.storage)).map(x => `<tr><td>${icon('storage')} ${esc(x.storage)}</td><td>${esc(x.type)}</td><td>${esc(x.content)}</td><td>${x.shared ? 'yes' : 'no'}</td><td style="width:220px">${x.total ? bar(pct(x.used, x.total)) : ''}</td><td>${x.total ? fmtBytes(x.used) + ' of ' + fmtBytes(x.total) : esc(x.active ? '' : 'inactive')}</td></tr>`).join('')}</table></div>`;
  return `<div class="grid2"><div class="box"><h3>${esc(name)} (Uptime: ${fmtUp(s.uptime)})</h3><div class="in">
    ${statrow('CPU usage', s.cpu * 100, `${(s.cpu * 100).toFixed(2)}% of ${s.cpuinfo.cpus} CPU(s)`)}
    ${statrow('IO delay', (s.wait || 0) * 100, ((s.wait || 0) * 100).toFixed(2) + '%')}
    <div class="statrow"><div class="lab">Load average</div><div>${esc((s.loadavg || []).join(', '))}</div><div></div></div>
    ${statrow('RAM usage', pct(s.memory.used, s.memory.total), `${pct(s.memory.used, s.memory.total).toFixed(2)}% (${fmtBytes(s.memory.used, 2)} of ${fmtBytes(s.memory.total, 2)})`)}
    ${s.swap ? statrow('SWAP usage', pct(s.swap.used, s.swap.total), `${pct(s.swap.used, s.swap.total).toFixed(2)}% (${fmtBytes(s.swap.used, 2)} of ${fmtBytes(s.swap.total, 2)})`) : ''}
    ${statrow('/ HD space', pct(s.rootfs.used, s.rootfs.total), `${pct(s.rootfs.used, s.rootfs.total).toFixed(2)}% (${fmtBytes(s.rootfs.used, 2)} of ${fmtBytes(s.rootfs.total, 2)})`)}
    </div></div><div class="box"><h3>System</h3><div class="in kv">
    <div>CPU(s)</div><div>${s.cpuinfo.cpus} x ${esc(s.cpuinfo.model)} (${s.cpuinfo.sockets} socket${s.cpuinfo.sockets > 1 ? 's' : ''})</div>
    <div>Kernel version</div><div>${esc(s['current-kernel'] ? s['current-kernel'].sysname + ' ' + s['current-kernel'].release + ' ' + s['current-kernel'].version : s.kversion)}</div>
    <div>Boot mode</div><div>${esc(s['boot-info'] ? s['boot-info'].mode + (s['boot-info'].secureboot ? ' (Secure Boot)' : '') : '-')}</div>
    <div>Manager version</div><div>${esc(s.pveversion)}</div><div>IP</div><div>${esc(r.ip)}</div>
    <div>Guests</div><div>${guestsByNode(name).length}</div></div></div></div>
    <div class="box"><h3>Graphs ${tfSel()}</h3><div class="in charts">${rrdCharts(r.rrd, 'node')}</div></div>`;
};

function cfgDisks(cfg) { return Object.keys(cfg).filter(k => /^(scsi|virtio|sata|ide|efidisk|tpmstate|unused|mp|rootfs)\d*$/.test(k)).sort(); }
function guestIPs(r) {
  const ips = [];
  for (const i of r.agent_net || []) for (const a of i['ip-addresses'] || []) if (a['ip-address-type'] === 'ipv4' && !a['ip-address'].startsWith('127.')) ips.push(a['ip-address']);
  for (const i of r.interfaces || []) if (i.inet) ips.push(i.inet.split('/')[0]);
  return [...new Set(ips)];
}
function jobsBoxes(j) {
  const rep = j.replication || [], bk = j.backup || [];
  return `<div class="box"><h3>Replication</h3>${rep.length ? `<table class="grid"><tr><th>Job</th><th>Source</th><th>Target</th><th>Schedule</th><th>Rate</th><th>Enabled</th></tr>${rep.map(x => `<tr><td class="mono">${esc(x.id)}</td><td>${esc(x.source)}</td><td>${esc(x.target)}</td><td class="mono">${esc(x.schedule || '*/15')}</td><td>${x.rate ? x.rate + ' MB/s' : '-'}</td><td>${x.disable ? 'no' : 'yes'}</td></tr>`).join('')}</table>` : '<div class="empty">No replication job for this guest.</div>'}</div>
    <div class="box"><h3>Scheduled backups</h3>${bk.length ? `<table class="grid"><tr><th>Job</th><th>Schedule</th><th>Storage</th><th>Mode</th><th>Retention</th><th>Enabled</th></tr>${bk.map(x => `<tr><td class="mono">${esc(x.id)}</td><td class="mono">${esc(x.schedule)}</td><td>${esc(x.storage)}</td><td>${esc(x.mode)}</td><td class="mono">${esc(x['prune-backups'] || '')}</td><td>${x.enabled == 0 ? 'no' : 'yes'}${x._pool_match_unknown ? ' (pool job, membership not checked)' : ''}</td></tr>`).join('')}</table>` : '<div class="empty">Not in any scheduled backup job.</div>'}</div>
    <div class="box"><h3>High availability</h3><div class="in">${j.ha ? `<div class="kv"><div>Resource</div><div class="mono">${esc(j.ha.sid)}</div><div>Requested state</div><div>${esc(j.ha.state)}</div><div>Group</div><div>${esc(j.ha.group || '-')}</div><div>Max restart / relocate</div><div>${esc(j.ha.max_restart ?? '-')} / ${esc(j.ha.max_relocate ?? '-')}</div></div>` : 'Not HA-managed.'}</div></div>`;
}

async function pveGuestPanel(n, sub) {
  const g = n.data; const r = await api('guest', { node: g.node, vmid: g.vmid, type: g.type, tf: S.tf });
  if (!r.ok) return `<div class="note err">${esc(r.error)}</div>`;
  const st = r.status, cfg = r.config; const parked = r.parked_on; const here = parked === r.this_host;
  const parkNote = parked ? `<div class="note pp"><b>Running on portable host '${esc(parked)}'.</b> This is the parked Proxmox copy: it must stay stopped. ${here ? `This host writes its changes back here on its sync schedule. <a href="#" data-goto="hvm/${g.vmid}">Open it on ${esc(parked)}</a>.` : `Manage it from the console of '${esc(parked)}'.`}</div>` : '';
  if (sub === 'summary') {
    const ips = guestIPs(r);
    return parkNote + `<div class="grid2"><div class="box"><h3>Status</h3><div class="in">
      <div class="kv" style="margin-bottom:8px"><div>Status</div><div>${pill(st.status)} ${st.qmpstatus && st.qmpstatus !== st.status ? '(' + esc(st.qmpstatus) + ')' : ''}</div>
      <div>HA state</div><div>${esc(st.ha && st.ha.managed ? st.ha.state : 'none')}</div><div>Node</div><div>${esc(g.node)}</div>
      ${st.uptime ? `<div>Uptime</div><div>${fmtUp(st.uptime)}</div>` : ''}<div>IPs</div><div>${ips.length ? esc(ips.join(', ')) : (st.status === 'running' ? '<span class="muted">guest agent not running</span>' : '-')}</div></div>
      ${statrow('CPU usage', (st.cpu || 0) * 100, `${((st.cpu || 0) * 100).toFixed(2)}% of ${st.cpus} CPU(s)`)}
      ${statrow('Memory usage', pct(st.mem, st.maxmem), `${pct(st.mem, st.maxmem).toFixed(2)}% (${fmtBytes(st.mem, 2)} of ${fmtBytes(st.maxmem, 2)})`)}
      ${g.type === 'lxc' ? statrow('Bootdisk size', pct(st.disk, st.maxdisk), `${fmtBytes(st.disk)} of ${fmtBytes(st.maxdisk)}`) : `<div class="statrow"><div class="lab">Bootdisk size</div><div>${fmtBytes(st.maxdisk)}</div><div></div></div>`}
      </div></div><div class="box"><h3>Notes</h3><div class="in prewrap">${esc(cfg.description || '') || '<span class="muted">none</span>'}</div></div></div>
      <div class="box"><h3>Graphs ${tfSel()}</h3><div class="in charts">${rrdCharts(r.rrd, 'guest')}</div></div>`;
  }
  if (sub === 'hardware' || sub === 'resources') {
    const keys = ['memory', 'swap', 'balloon', 'sockets', 'cores', 'cpu', 'bios', 'machine', 'vga', 'scsihw', ...cfgDisks(cfg), ...Object.keys(cfg).filter(k => /^(net|usb|hostpci|serial|audio|rng)\d+$/.test(k)).sort()];
    return parkNote + `<div class="box"><h3>${sub === 'hardware' ? 'Hardware' : 'Resources'}</h3><table class="grid">${keys.filter(k => cfg[k] != null).map(k => `<tr><th class="kcol">${esc(k)}</th><td class="mono">${esc(cfg[k])}</td></tr>`).join('')}</table></div>`;
  }
  if (sub === 'options') {
    const skip = new Set(['digest', 'description']);
    return parkNote + `<div class="box"><h3>Options (full config)</h3><table class="grid">${Object.keys(cfg).sort().filter(k => !skip.has(k)).map(k => `<tr><th class="kcol">${esc(k)}</th><td class="mono">${esc(cfg[k])}</td></tr>`).join('')}</table></div>`;
  }
  if (sub === 'replication' || sub === 'backup') return parkNote + jobsBoxes(r.jobs);
  if (sub === 'snapshots') return parkNote + `<div class="box"><h3>Snapshots</h3>${(r.snapshots || []).filter(s => s.name !== 'current').length ? `<table class="grid"><tr><th>Name</th><th>Date</th><th>RAM</th><th>Description</th></tr>${r.snapshots.filter(s => s.name !== 'current').map(s => `<tr><td>${esc(s.name)}</td><td>${fmtTime(s.snaptime)}</td><td>${s.vmstate ? 'yes' : 'no'}</td><td>${esc(s.description || '')}</td></tr>`).join('')}</table>` : '<div class="empty">No snapshots.</div>'}</div>`;
  if (sub === 'portable') return movePanel(g, r);
}
PANELS.qemu = pveGuestPanel;
PANELS.lxc = pveGuestPanel;

function movePanel(g, r) {
  const ct = g.type === 'lxc'; const what = ct ? 'CT' : 'VM';
  if (r.parked_on) return r.parked_on === r.this_host
    ? `<div class="note pp"><b>Already on this host.</b> <a href="#" data-goto="hvm/${g.vmid}">Open ${what} ${g.vmid} on ${esc(HN())}</a>.</div>`
    : `<div class="note pp"><b>This ${what} runs on another portable host, '${esc(r.parked_on)}'.</b> Return it from that host's console first.</div>`;
  const ips = guestIPs(r);
  if (ct && !ips.length) { const mm = String(r.config.net0 || '').match(/ip=(\d+\.\d+\.\d+\.\d+)/); if (mm) ips.push(mm[1]); }
  const bk = (r.jobs.backup || []).length, rep = (r.jobs.replication || []).length;
  const running = g.status === 'running'; const h = S.tree.host || {};
  return `<div class="box"><h3>Move ${what} ${g.vmid} (${esc(g.name)}) to ${esc(HN())}</h3><div class="in">
    <div class="note">${ct
      ? `Containers move in <b>restart mode</b> (Proxmox can't live-migrate containers either): the root filesystem is pre-copied with rsync while it keeps running on <b>${esc(g.node)}</b>, then it is stopped, the last changes are copied, and it starts under LXC here. Downtime is the stop + final delta + start, typically 10-30 s.`
      : `The VM runs here under <b>Proxmox's own QEMU</b> with the exact command line Proxmox builds, so its hardware (machine type, UEFI, TPM, MACs, UUID) is identical. <b>Live</b>: disks are mirrored and RAM migrated while it runs, ~0.1 s pause, like <span class="mono">qm migrate --online</span>. <b>Cold</b>: shut down, copy, boot here.`}
      The Proxmox copy stays on ${esc(g.node)}, stopped and tagged, and this host writes its changes back to it on the sync schedule.</div>
    <div class="form" id="mig-form">
      ${ct ? '' : `<label>Move type</label><select id="m-move"><option value="live" ${running ? '' : 'disabled'}>Live (no downtime)</option><option value="cold" ${running ? '' : 'selected'}>Cold (shut down, copy, boot)</option></select>
      <div class="help" id="m-movehelp">${running ? 'Checking whether a live move is possible…' : 'Not running: cold move.'}</div>
      <label id="m-riskl" hidden>CPU risk</label><label id="m-riskw" hidden><input type="checkbox" id="m-risk"> I accept that a live move of a CPU type <i>host</i> VM between different CPUs can crash it</label>`}
      <label>Network</label><select id="m-net"><option value="lan">LAN, keep its IP (routed, proxy ARP)</option><option value="nat">NAT behind this host (new ${esc(h.nat_network || '')} address)</option></select>
      <div class="help">LAN mode works over Wi-Fi as well as wired: this host answers ARP for the guest's IP, routes to it and hands it its old address over DHCP. Only useful while this host is on the guest's LAN. NAT works anywhere (the guest must use DHCP).</div>
      <label>Guest LAN IP</label><input type="text" id="m-ip" value="${esc(ips[0] || '')}" placeholder="the guest's IPv4 address">
      <div class="help">${ips.length ? 'Read from the running guest.' : 'Could not read the IP; enter the address it uses.'}</div>
      <label>Sync back to ${esc(g.node)}</label><select id="m-sync"><option value="15min">every 15 minutes</option><option value="30min">every 30 minutes</option><option value="hourly" selected>hourly</option><option value="2h">every 2 hours</option><option value="6h">every 6 hours</option><option value="daily">daily</option><option value="off">off (manual only)</option></select>
      <div class="help">${ct ? 'Each sync rsyncs only changed files (two passes, the second with the container frozen for a moment).' : 'Each sync ships only the blocks changed since the last one.'} ${rep ? `Proxmox replication (${rep} job${rep > 1 ? 's' : ''}) then carries them to the other nodes.` : 'No Proxmox replication job exists for it.'} ${bk ? `Scheduled backups (${bk} job${bk > 1 ? 's' : ''}) back up the parked copy as of its last sync.` : 'It is not in any scheduled backup job.'}</div>
      <label>Shutdown timeout (s)</label><input type="number" id="m-tmo" value="300" min="30" max="1800">
      <label>If it won't shut down</label><select id="m-force"><option value="0">abort the move (safe)</option><option value="1">force stop (like pulling the plug)</option></select>
      <label>Start here</label><select id="m-start"><option value="1">yes</option><option value="0">no, leave it stopped</option></select>
      <label></label><div><button class="btn" id="m-pre">Check</button> <button class="btn pp" id="m-go" disabled>⇣ Move to ${esc(HN())}</button></div>
    </div><div id="m-preout"></div></div></div>` + jobsBoxes(r.jobs);
}

PANELS.host = async (n, sub) => {
  if (sub === 'guide') return guide();
  const r = await api('host');
  const s = r.status || {};
  if (!r.ok) return `<div class="note err">${esc(r.error)}</div>`;
  if (sub === 'guests') return `<div class="box"><h3>Guests on ${esc(s.name)}</h3>${guestTable(s.guests.filter(g => !['returned', 'failed-import'].includes(g.pp_state)).map(g => ({ ...g, type: g.kind || 'qemu', tags: '' })), true)}</div>
    ${s.guests.some(g => ['returned', 'failed-import'].includes(g.pp_state)) ? `<div class="box"><h3>History (returned or failed)</h3><table class="grid"><tr><th>ID</th><th>Name</th><th>State</th><th>From</th></tr>${s.guests.filter(g => ['returned', 'failed-import'].includes(g.pp_state)).map(g => `<tr><td>${g.vmid}</td><td>${esc(g.name)}</td><td>${pill(g.pp_state)}</td><td>${esc(g.origin_node)}</td></tr>`).join('')}</table></div>` : ''}`;
  if (sub === 'network') return `<div class="box"><h3>Interfaces</h3><table class="grid"><tr><th>Name</th><th>State</th><th>MAC</th><th>IPv4</th></tr>${s.ifaces.map(i => `<tr><td class="mono">${esc(i.name)}${i.name === s.lan_iface ? ' <span class="pill">LAN</span>' : ''}</td><td>${esc(i.state)}</td><td class="mono">${esc(i.mac)}</td><td class="mono">${esc(i.addrs.join(', '))}</td></tr>`).join('')}</table></div>
    <div class="note"><span class="mono">${esc(s.bridge)}</span> is the routed bridge for LAN-mode guests (proxy ARP on <span class="mono">${esc(s.lan_iface)}</span>); <span class="mono">${esc(s.nat_bridge)}</span> is the NAT bridge (${esc(s.nat_network)}, DHCP and DNS from this host).</div>`;
  if (sub === 'jobs') return jobTable(r.jobs);
  return `<div class="grid2"><div class="box"><h3>${esc(s.name)} (Uptime: ${fmtUp(s.uptime)})</h3><div class="in">
    ${statrow('CPU usage', s.cpu * 100, `${(s.cpu * 100).toFixed(2)}% of ${s.cpus} CPU(s)`)}
    <div class="statrow"><div class="lab">Load average</div><div>${esc(s.loadavg.join(', '))}</div><div></div></div>
    ${statrow('RAM usage', pct(s.mem.used, s.mem.total), `${pct(s.mem.used, s.mem.total).toFixed(2)}% (${fmtBytes(s.mem.used, 2)} of ${fmtBytes(s.mem.total, 2)})`)}
    ${s.swap.total ? statrow('SWAP usage', pct(s.swap.used, s.swap.total), `${pct(s.swap.used, s.swap.total).toFixed(2)}% (${fmtBytes(s.swap.used, 2)} of ${fmtBytes(s.swap.total, 2)})`) : ''}
    ${statrow('/ HD space', pct(s.rootfs.used, s.rootfs.total), `${pct(s.rootfs.used, s.rootfs.total).toFixed(2)}% (${fmtBytes(s.rootfs.used, 2)} of ${fmtBytes(s.rootfs.total, 2)})`)}
    ${s.datafs ? statrow('Guest storage', pct(s.datafs.used, s.datafs.total), `${fmtBytes(s.datafs.free)} free in ${esc(s.data_dir)}`) : ''}
    ${s.battery ? `<div class="statrow"><div class="lab">Battery</div>${bar(+s.battery.capacity)}<div>${esc(s.battery.capacity)}% ${esc(s.battery.status)}</div></div>` : ''}
    </div></div><div class="box"><h3>System</h3><div class="in kv">
    <div>CPU(s)</div><div>${s.cpus} x ${esc(s.cpu_model)}</div><div>KVM</div><div>${s.kvm ? 'available' : '<span class="pill err">/dev/kvm missing</span>'}</div>
    <div>OS</div><div>${esc(s.os)}</div><div>Kernel version</div><div>${esc(s.kernel)}</div>
    <div>Hypervisor</div><div>${esc((s.qemu || 'Proxmox QEMU missing: run fetch-qemu').replace('QEMU emulator version ', 'Proxmox QEMU '))}; LXC ${esc(s.lxc || '-')}</div>
    <div>proxmox-portable</div><div>${esc(s.version)}</div><div>Cluster tag</div><div class="mono">${esc(s.tag)}</div>
    ${s.tailscale ? `<div>Tailscale</div><div class="mono">${esc(s.tailscale)}</div>` : ''}
    <div>Guests</div><div>${s.guests.filter(g => g.pp_state === 'on-host').length}</div></div></div></div>
    <div class="box"><h3>Graphs (last 24 h, 1-minute samples)</h3><div class="in charts">${histCharts(r.history, true)}</div></div>`;
};

PANELS.hvm = async (n, sub) => {
  const g = n.data;
  const r = await api('lguest', { vmid: g.vmid });
  S.detail = r;
  const v = r.vm || {};
  if (!v.ok) return `<div class="note err">${esc(v.error || r.error)}</div>`;
  const m = v.meta; const ct = m.kind === 'lxc'; const what = ct ? 'CT' : 'VM';
  const last = (r.history || []).slice(-1)[0] || {};
  if (sub === 'summary') {
    return `<div class="grid2"><div class="box"><h3>Status</h3><div class="in">
      <div class="kv" style="margin-bottom:8px"><div>Status</div><div>${pill(v.status)} ${pill(m.state)}</div>
      <div>Came from</div><div>node <a href="#" data-goto="${ct ? 'lxc' : 'qemu'}/${m.vmid}">${esc(m.origin_node)}</a> (${esc(m.origin_ip)}), ${fmtTime(m.imported)} - ${esc(m.move_type || 'cold')} move</div>
      <div>Runtime</div><div>${ct ? 'LXC (Debian)' : `Proxmox QEMU, machine <span class="mono">${esc(m.machine || '?')}</span>`}</div>
      <div>IPs</div><div>${v.ips && v.ips.ips.length ? esc(v.ips.ips.join(', ')) + ` <span class="muted">(${esc(v.ips.source)})</span>` : '-'}</div>
      <div>Network</div><div>${m.net_mode === 'lan' ? 'LAN, routed, keeps ' + esc(m.guest_ip) : 'NAT'}</div>
      <div>Last sync</div><div>${m.syncs && m.syncs.length ? ago(m.syncs[m.syncs.length - 1].time) : 'never'} (${esc(m.sync_interval)})</div></div>
      ${statrow('CPU usage', (last.cpu || 0) * 100, `${((last.cpu || 0) * 100).toFixed(2)}% of ${m.vcpus} CPU(s)`)}
      ${statrow('Memory', last.memtotal ? pct(last.mem, last.memtotal) : 0, last.memtotal ? `${fmtBytes(last.mem)} of ${fmtBytes(last.memtotal)}` : fmtBytes(m.memory_mb * 2 ** 20))}
      </div></div>${ct ? `<div class="box"><h3>Root filesystem</h3><div class="in kv"><div>Here</div><div class="mono">${esc((S.tree.host || {}).data_dir)}/vms/${m.vmid}/rootfs</div><div>Sync method</div><div>rsync (changed files only)</div><div>Proxmox volume</div><div class="mono">${esc(m.pve_config.rootfs)}</div></div></div>`
      : `<div class="box"><h3>Volumes</h3><table class="grid"><tr><th>Volume</th><th>Kind</th><th>Size</th><th>Pending sync</th></tr>${m.disks.filter(d => d.base).map(d => { const ch = (v.chain || {})[d.key] || []; return `<tr><td class="mono">${esc(d.key)}</td><td>${esc(d.kind)}</td><td>${fmtBytes(d.size)}</td><td>${d.kind === 'disk' ? fmtBytes(ch.slice(1).reduce((a, f) => a + (f.alloc || 0), 0)) : d.kind === 'efi' || d.kind === 'tpm' ? 'synced when changed' : '-'}</td></tr>`; }).join('')}</table></div>`}</div>
      <div class="box"><h3>Graphs (last 24 h, 1-minute samples)</h3><div class="in charts">${histCharts(r.history, false)}</div></div>`;
  }
  if (sub === 'hardware') {
    const cfg = m.pve_config;
    const keys = Object.keys(cfg).filter(k => !['digest', 'description', 'meta', 'vmgenid', 'smbios1'].includes(k)).sort();
    const drop = keys.filter(k => /^hostpci|^dev\d/.test(k) || (/^usb\d/.test(k) && !/host=[0-9a-f]{4}:[0-9a-f]{4}|spice/i.test(cfg[k])));
    return `<div class="note">The Proxmox config at the time of the move. ${ct ? 'Converted to an LXC config (same MAC, idmap, memory, cores, features).' : 'This host runs Proxmox\'s own command line for it, so the virtual hardware is identical.'}${drop.length ? ` <b>Not here</b> (restored on return): ${esc(drop.join(', '))}.` : ''}</div>
      <div class="box"><h3>${ct ? 'Resources' : 'Hardware'}</h3><table class="grid">${keys.map(k => `<tr><th class="kcol">${esc(k)}</th><td class="mono">${esc(cfg[k])}${drop.includes(k) ? ' <span class="pill err">stays in the node</span>' : ''}</td></tr>`).join('')}</table></div>
      ${ct ? '' : `<div class="box"><h3>Disk chains on ${esc(HN())}</h3><table class="grid"><tr><th>Disk</th><th>File</th><th>Allocated</th></tr>${Object.entries(v.chain || {}).flatMap(([k, files]) => files.map((f, i) => `<tr><td class="mono">${i ? '' : esc(k)}</td><td class="mono">${'&nbsp;'.repeat(i * 3)}${i ? '└ ' : ''}${esc(f.file)}</td><td>${fmtBytes(f.alloc)}</td></tr>`)).join('')}</table></div>`}`;
  }
  if (sub === 'sync') {
    const syncs = (m.syncs || []).slice().reverse();
    return `<div class="box"><h3>Sync back to ${esc(m.origin_node)}</h3><div class="in"><div class="form">
      <label>Schedule</label><div><select id="s-iv">${['15min', '30min', 'hourly', '2h', '6h', 'daily', 'off'].map(x => `<option ${x === m.sync_interval ? 'selected' : ''}>${x}</option>`).join('')}</select> <button class="btn" id="s-save">Save</button> <button class="btn primary" id="s-now">⇅ Sync now</button></div>
      <div class="help">${ct ? `rsyncs the root filesystem into the parked container on ${esc(m.origin_node)}: a first pass while it runs, then a short second pass with the container frozen, holding Proxmox's replication lock and the container mounted.`
        : `Freezes the disks (guest-agent quiesce when available), ships only the changed blocks to ${esc(m.origin_node)} and writes them into the parked VM's disks while holding Proxmox's replication lock and <span class="mono">lock: migrate</span>; changed EFI vars and TPM state go in the same window. Then it folds them into the local base image.`} Scheduled syncs run while the console service runs.</div></div></div></div>
      <div class="box"><h3>Sync history</h3>${syncs.length ? `<table class="grid"><tr><th>When</th><th>Changed data</th><th>Took</th><th>Guest was</th></tr>${syncs.slice(0, 50).map(s => `<tr><td>${fmtTime(s.time)} <span class="muted">(${ago(s.time)})</span></td><td>${fmtBytes(s.bytes)}</td><td>${s.seconds}s</td><td>${s.running ? 'running' : 'stopped'}</td></tr>`).join('')}</table>` : '<div class="empty">No sync yet.</div>'}</div>
      <div class="note">After each sync the parked copy on ${esc(m.origin_node)} matches this host as of that moment, so Proxmox's own jobs keep working on it:</div>` + (r.pvejobs ? jobsBoxes(r.pvejobs) : '');
  }
  if (sub === 'return') {
    const canLive = !ct && v.status === 'running';
    return `<div class="box"><h3>Return ${what} ${m.vmid} (${esc(m.name)}) to ${esc(m.origin_node)}</h3><div class="in">
      ${ct ? `<ol class="steps"><li>Pre-sync while it runs.</li><li>Stop it here, copy the last changes.</li><li>Restore tags, <span class="mono">onboot=${esc(m.pve_onboot || 0)}</span>${m.pve_ha ? ', HA' : ''} and start it on ${esc(m.origin_node)}.</li></ol>`
        : `<p><b>Live</b>: sync, then start a target QEMU on ${esc(m.origin_node)} (on the parked disks), mirror only the blocks changed since that sync, migrate RAM, ~0.1 s pause. <b>Cold</b>: shut down, final sync, optional full sha256 verify, start on ${esc(m.origin_node)}.</p>`}
      <div class="form" style="margin-top:10px">
      ${ct ? '' : `<label>Move type</label><select id="r-move"><option value="live" ${canLive ? 'selected' : 'disabled'}>Live (no downtime)</option><option value="cold" ${canLive ? '' : 'selected'}>Cold (shut down first)</option></select>`}
      <label>Start on ${esc(m.origin_node)}</label><select id="r-start"><option value="1">yes</option><option value="0">no, leave it stopped</option></select>
      ${ct ? '' : '<label>Verify checksums (cold)</label><select id="r-verify"><option value="1">yes (recommended)</option><option value="0">no</option></select>'}
      <label>Local copy afterwards</label><select id="r-del"><option value="1">delete it</option><option value="0">keep the files</option></select>
      <label>Shutdown timeout (s)</label><input type="number" id="r-tmo" value="180" min="30" max="1800">
      <label>If it won't shut down</label><select id="r-force"><option value="0">abort (safe)</option><option value="1">force off</option></select>
      <label></label><div><button class="btn primary" id="r-go">⤺ Return to Proxmox</button></div></div></div></div>
      <div class="note">To go back to a <i>different</i> node, return it to ${esc(m.origin_node)} first, then use Proxmox's normal Migrate. If a step fails it stays on this host (a failed live return resumes it here).</div>`;
  }
  if (sub === 'jobs') return jobTable(r.jobs || []);
};

function jobTable(jobs) {
  if (!jobs.length) return '<div class="empty">No jobs.</div>';
  return `<table class="grid"><tr><th>Started</th><th>Ended</th><th>Guest</th><th>Job</th><th>Status</th><th>Progress</th></tr>${jobs.map(j => {
    const p = j.progress; const prog = j.status === 'running' && p ? `${esc(p.step)} ${p.total ? (100 * p.done / p.total).toFixed(0) + '%' : ''} ${p.rate ? fmtRate(p.rate) : ''}` : (j.error ? `<span class="muted">${esc(j.error).slice(0, 140)}</span>` : '');
    return `<tr class="click" data-job="${esc(j.id)}"><td>${fmtTime(j.started || j.created)}</td><td>${j.ended ? fmtTime(j.ended) : ''}</td><td>${j.vmid}</td><td>${esc(j.kind)}${j.timer ? ' (scheduled)' : ''}</td><td>${pill(j.status)}</td><td>${prog}</td></tr>`; }).join('')}</table>`;
}

// ------------------------------------------------------------- panel bindings
function bindPanel(k, n) {
  const tf = $('#tf'); if (tf) tf.onchange = () => { S.tf = tf.value; renderView(true); };
  if ((k === 'qemu' || k === 'lxc') && S.sub === 'portable' && $('#m-pre')) {
    const g = n.data; const ct = k === 'lxc';
    let pf = null;
    const moveSync = () => {
      if (ct || !pf) return;
      const live = $('#m-move').value === 'live';
      const cpuRisk = (pf.live_warnings || []).some(w => w.startsWith('CPU type'));
      $('#m-riskl').hidden = $('#m-riskw').hidden = !(live && cpuRisk);
      $('#m-movehelp').innerHTML = live ? (pf.live_ok ? 'Live move possible.' : '<b>Live not possible:</b> ' + esc(pf.live_problems.join('; '))) + (pf.live_warnings.length ? '<br>' + esc(pf.live_warnings.join(' ')) : '')
        : 'Shuts it down on ' + esc(g.node) + ', copies, boots it here.';
      $('#m-go').disabled = !!pf.problems.length || (live && (!pf.live_ok || (cpuRisk && !$('#m-risk').checked)));
    };
    const pre = async () => {
      $('#m-go').disabled = true; $('#m-preout').innerHTML = '<div class="note">Checking…</div>';
      const r = await api('preflight', { node: g.node, vmid: g.vmid, kind: ct ? 'lxc' : 'qemu' });
      if (!r.ok) { $('#m-preout').innerHTML = `<div class="note err">${esc(r.error)}</div>`; return; }
      pf = r;
      if (!ct && !r.live_ok && $('#m-move').value === 'live') $('#m-move').value = 'cold';
      const okk = !r.problems.length;
      $('#m-preout').innerHTML = `<div class="note ${okk ? '' : 'err'}">${okk ? `<b>${esc(r.host)} can take it.</b> Needs ~${fmtBytes(r.need)} of ${fmtBytes(r.free)} free.` : '<b>Cannot move:</b><ul>' + r.problems.map(p => `<li>${esc(p)}</li>`).join('') + '</ul>'}
        ${r.warnings.length ? '<ul>' + r.warnings.map(p => `<li>${esc(p)}</li>`).join('') + '</ul>' : ''}</div>
        <div class="box"><h3>Disks</h3><table class="grid"><tr><th>Disk</th><th>Volume</th><th>Size</th><th>Used</th><th>Action</th></tr>${r.disks.map(d => `<tr><td class="mono">${esc(d.key)}</td><td class="mono">${esc(d.volid)}</td><td>${d.size ? fmtBytes(d.size) : '-'}</td><td>${d.used ? fmtBytes(d.used) : '-'}</td><td>${esc(d.action)}</td></tr>`).join('')}</table></div>`;
      $('#m-go').disabled = !okk;
      moveSync();
    };
    $('#m-pre').onclick = pre;
    if (!ct) { $('#m-move').onchange = moveSync; $('#m-risk').onchange = moveSync; }
    $('#m-net').onchange = () => { $('#m-ip').disabled = $('#m-net').value !== 'lan'; };
    $('#m-go').onclick = async () => {
      if ($('#m-net').value === 'lan' && !/^\d+\.\d+\.\d+\.\d+$/.test($('#m-ip').value)) { toast('Enter the guest LAN IP for LAN mode', true); return; }
      const move = ct ? 'cold' : $('#m-move').value;
      const how = move === 'live' ? 'live, it keeps running' : ct ? 'it restarts here' : `it is shut down on ${g.node}`;
      if (!await confirmTyped(`Move ${ct ? 'CT' : 'VM'} ${g.vmid} (${g.name}) from ${g.node} to ${HN()} (${how})?`, String(g.vmid))) return;
      const r = await api('import', { node: g.node, vmid: g.vmid, kind: ct ? 'lxc' : 'qemu', move, accept_cpu_risk: !ct && $('#m-risk').checked ? 1 : 0,
        net_mode: $('#m-net').value, guest_ip: $('#m-ip').value,
        sync_interval: $('#m-sync').value, start: $('#m-start').value, force_stop: $('#m-force').value, shutdown_timeout: $('#m-tmo').value }, true);
      if (!r.ok) { toast(r.error, true); return; }
      toast('Move job started: ' + r.job); openJob(r.job); refresh();
    };
    pre();
  }
  if (k === 'hvm' && S.sub === 'sync' && $('#s-save')) {
    const g = n.data;
    $('#s-save').onclick = async () => { const r = await api('setsync', { vmid: g.vmid, interval: $('#s-iv').value }, true); toast(r.ok ? 'Schedule saved: ' + r.sync_interval : r.error, !r.ok); };
    $('#s-now').onclick = async () => { const r = await api('sync', { vmid: g.vmid }, true); toast(r.ok ? 'Sync job started' : r.error, !r.ok); if (r.ok) openJob(r.job); };
  }
  if (k === 'hvm' && S.sub === 'return' && $('#r-go')) {
    const g = n.data;
    $('#r-go').onclick = async () => {
      const move = $('#r-move') ? $('#r-move').value : 'cold';
      if (!await confirmTyped(`Return ${g.vmid} (${g.name}) from ${HN()} to Proxmox (${move === 'live' ? 'live, it keeps running' : 'it is stopped here first'})?`, String(g.vmid))) return;
      const r = await api('return', { vmid: g.vmid, move, start: $('#r-start').value, verify: $('#r-verify') ? $('#r-verify').value : 0, delete_local: $('#r-del').value, force_stop: $('#r-force').value, shutdown_timeout: $('#r-tmo').value }, true);
      if (!r.ok) { toast(r.error, true); return; }
      toast('Return job started: ' + r.job); openJob(r.job);
    };
  }
}
document.addEventListener('click', e => {
  const t = e.target.closest('[data-goto]');
  if (t && !t.closest('#toolbar')) { e.preventDefault(); select(t.dataset.goto); return; }
  const j = e.target.closest('[data-job]');
  if (j) { openJob(j.dataset.job); return; }
  const u = e.target.closest('[data-upid]');
  if (u) openPveTask(u.dataset.node, u.dataset.upid);
});

// ------------------------------------------------------------- modal / toast
let modalTimer = null;
function openModal(title, body, foot = '') { $('#m-title').textContent = title; $('#m-body').innerHTML = body; $('#m-foot').innerHTML = foot; $('#modal-bg').hidden = false; }
function closeModal() { $('#modal-bg').hidden = true; clearInterval(modalTimer); modalTimer = null; if (S._confirm) { S._confirm(false); S._confirm = null; } }
$('#m-close').onclick = closeModal;
$('#modal-bg').addEventListener('click', e => { if (e.target.id === 'modal-bg') closeModal(); });
function confirmTyped(msg, typed) {
  return new Promise(res => {
    openModal('Confirm', `<p>${esc(msg)}</p>${typed ? `<p>Type <b class="mono">${esc(typed)}</b> to confirm:</p><input id="c-in" type="text" class="cin">` : ''}`,
      `<button class="btn" id="c-no">Cancel</button><button class="btn primary" id="c-yes" ${typed ? 'disabled' : ''}>Yes</button>`);
    S._confirm = res;
    if (typed) { $('#c-in').focus(); $('#c-in').oninput = () => { $('#c-yes').disabled = $('#c-in').value.trim() !== typed; }; $('#c-in').onkeydown = e => { if (e.key === 'Enter' && !$('#c-yes').disabled) $('#c-yes').click(); }; }
    $('#c-no').onclick = () => { S._confirm = null; closeModal(); res(false); };
    $('#c-yes').onclick = () => { S._confirm = null; closeModal(); res(true); };
  });
}
function openJob(id) {
  const load = async () => {
    const r = await api('job', { id });
    if (!r.ok) { $('#m-body').innerHTML = `<div class="note err">${esc(r.error)}</div>`; return; }
    const j = r.job; const p = j.progress;
    const pre = $('#m-body pre'); const atBottom = !pre || pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 10;
    $('#m-body').innerHTML = `<div class="kv" style="margin-bottom:8px"><div>Guest</div><div>${j.vmid}</div><div>Status</div><div>${pill(j.status)} ${j.error ? esc(j.error) : ''}</div>
      ${j.status === 'running' && p ? `<div>Step</div><div>${esc(p.step)} ${p.total ? `${fmtBytes(p.done)} / ${fmtBytes(p.total)} (${(100 * p.done / p.total).toFixed(0)}%)` : ''} ${p.rate ? fmtRate(p.rate) : ''}</div>` : ''}</div>
      ${j.status === 'running' && p && p.total ? bar(100 * p.done / p.total) : ''}<pre class="log" style="margin-top:8px">${esc(j.log || '')}</pre>`;
    const np = $('#m-body pre'); if (atBottom) np.scrollTop = np.scrollHeight;
    if (j.status !== 'running' && j.status !== 'queued') { clearInterval(modalTimer); modalTimer = null; refresh(); }
  };
  openModal(`Job ${id}`, '<div class="empty">Loading…</div>');
  load(); clearInterval(modalTimer); modalTimer = setInterval(load, 2000);
}
async function openPveTask(node, upid) {
  openModal('Task ' + upid, '<div class="empty">Loading…</div>');
  const r = await api('pvetask', { node, upid });
  $('#m-body').innerHTML = r.ok ? `<div class="kv"><div>Status</div><div>${esc(r.status.status)} ${esc(r.status.exitstatus || '')}</div></div><pre class="log">${esc((r.log || []).map(l => l.t).join('\n'))}</pre>` : `<div class="note err">${esc(r.error)}</div>`;
}
function toast(msg, err) {
  const d = document.createElement('div');
  d.className = 'toast' + (err ? ' err' : '');
  d.textContent = msg; document.body.appendChild(d); setTimeout(() => d.remove(), err ? 9000 : 4500);
}

// ------------------------------------------------------------- tasks panel
async function refreshJobs() { const r = await api('jobs'); if (!r.ok) return; S.jobs = r; renderTasks(); }
function renderTasks() {
  const r = S.jobs; if (!r) return;
  $$('#tasks .tab').forEach(t => t.classList.toggle('active', t.dataset.t === S.taskTab));
  if (S.taskTab === 'pp') { $('#task-list').innerHTML = jobTable(r.jobs || []); return; }
  $('#task-list').innerHTML = `<table class="grid"><tr><th>Start time</th><th>End time</th><th>Node</th><th>User</th><th>Description</th><th>Status</th></tr>${(r.tasks || []).map(t => `<tr class="click" data-node="${esc(t.node)}" data-upid="${esc(t.upid)}"><td>${fmtTime(t.starttime)}</td><td>${t.endtime ? fmtTime(t.endtime) : ''}</td><td>${esc(t.node)}</td><td>${esc(t.user)}</td><td>${esc(t.type)} ${esc(t.id || '')}</td><td>${t.status ? (t.status === 'OK' ? pill('OK') : `<span class="pill err">${esc(t.status)}</span>`) : pill('running')}</td></tr>`).join('')}</table>`;
}
$$('#tasks .tab').forEach(t => t.onclick = () => { S.taskTab = t.dataset.t; renderTasks(); });

// ------------------------------------------------------------- splitters, theme
function splitter(el, prop, axis, min, max) {
  el.addEventListener('mousedown', e => {
    e.preventDefault();
    const mv = ev => { let v = axis === 'x' ? ev.clientX : window.innerHeight - ev.clientY; v = Math.max(min, Math.min(max, v)); document.documentElement.style.setProperty(prop, v + 'px'); };
    const up = () => { document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up); localSet('pp' + prop, getComputedStyle(document.documentElement).getPropertyValue(prop)); drawCharts(); };
    document.addEventListener('mousemove', mv); document.addEventListener('mouseup', up);
  });
}
for (const p of ['--tree-w', '--tasks-h']) { const v = localGet('pp' + p); if (v) document.documentElement.style.setProperty(p, v); }
splitter($('#split-v'), '--tree-w', 'x', 180, 600);
splitter($('#split-h'), '--tasks-h', 'y', 60, 600);
const th = localGet('pp-theme'); if (th) document.documentElement.dataset.theme = th;
$('#theme').onclick = () => {
  const cur = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  document.documentElement.dataset.theme = cur === 'dark' ? 'light' : 'dark'; localSet('pp-theme', document.documentElement.dataset.theme); renderView(false);
};

// ------------------------------------------------------------- guide
function guide() {
  const h = (S.tree && S.tree.host) || {}; const tp = esc((S.tree && S.tree.tag_prefix) || 'portable-');
  return `<div class="box"><h3>How proxmox-portable works</h3><div class="in">
<p><b>This host</b> (${esc(h.name)}) runs Debian with <b>Proxmox's own QEMU</b> (pve-qemu-kvm, swtpm and OVMF copied from the cluster and unpacked beside Debian's packages) and Debian LXC. It never joins corosync, so it can't affect quorum. One script is the console you are looking at, the hypervisor side and the mover.</p>
<p><b>Same hardware here.</b> A VM runs from the exact command line Proxmox builds for it (<span class="mono">qm showcmd</span>), with only the disk paths, tap scripts and passthrough devices changed. Machine type, UEFI firmware and vars, TPM, MACs, SMBIOS UUID and vmgenid are identical, so Windows activation, BitLocker/TPM and NIC names don't notice.</p>
<p><b>Live move</b> (VM running, no passthrough, no pending config changes): the source tracks writes with a dirty bitmap while this host pre-seeds a compressed copy of each disk; then this host starts the VM's QEMU with <span class="mono">-incoming</span> and exports its disks over NBD through an SSH tunnel, the node mirrors only the blocks written since the pre-seed, RAM is migrated and the guest continues here after a ~0.1-0.3 s pause. Auto-converge, a stepped downtime limit and a write-blocking mirror fallback make it finish over slow links. CPU type <i>host</i> needs an explicit risk acknowledgement.</p>
<p><b>Cold move</b>: shut down (through HA when HA manages it), stream every volume with sha256 checks, boot here. Passthrough: USB by vendor:product is kept (it attaches if that device is plugged in here); PCI, port-based USB and host serial ports are dropped here and come back on return, because the Proxmox config is never edited beyond tags and onboot.</p>
<p><b>Containers</b> move in restart mode (Proxmox can't live-migrate containers either): rsync pre-copy while running, stop, final delta, start under LXC with the same MAC, idmap and limits. Host bind mounts are not copied.</p>
<p><b>The parked copy.</b> The Proxmox guest stays on its node, stopped, <span class="mono">onboot=0</span>, tag <span class="mono">${tp}${esc(h.name)}</span>, out of HA.</p>
<p><b>Sync = changed blocks only.</b> Here each VM disk is a raw <i>base</i> (identical to the parked disk) plus a qcow2 <i>overlay</i> holding every write since. A sync takes a QMP snapshot (with guest-agent freeze), ships the frozen overlay and writes it into the parked disk under Proxmox's replication lock and <span class="mono">lock: migrate</span>, plus changed EFI vars and TPM state, then folds it into the base with a live block-commit. Containers sync with rsync, the second pass frozen. Proxmox replication then copies the changes to the replicas and scheduled backups back up the parked copy as of the last sync.</p>
<p><b>Return.</b> <i>Live</i>: sync, start a target QEMU on the home node on the parked disks under its normal <span class="mono">&lt;vmid&gt;.scope</span>, mirror only the overlay, migrate RAM back, restore tags/onboot/HA. <i>Cold</i>: shut down, final sync, optional full sha256 verify, start. If a live step fails, the guest keeps running where it was.</p>
<p><b>Networking.</b> <i>LAN</i> mode keeps the guest's IP: this host routes it to bridge <span class="mono">${esc(h.bridge)}</span>, answers ARP for it (works on Wi-Fi), hands out its old address by DHCP, rewrites frames from guests to its own MAC (so a live-moved guest with the router's MAC cached keeps working) and sends gratuitous ARP when it arrives. <i>NAT</i> mode uses bridge <span class="mono">${esc(h.nat_bridge)}</span> (${esc(h.nat_network)}) and works anywhere.</p>
<p><b>Safety rails.</b> This host never starts or syncs a guest whose Proxmox copy is running, or whose <span class="mono">${tp}${esc(h.name)}</span> tag was removed (taken back by hand). This console blocks Start on parked guests; Proxmox's own UI doesn't know, so don't start a <span class="mono">${tp}*</span> guest there. Limits: raw volumes only (ZFS/LVM/raw files, not qcow2); live moves need the Proxmox QEMU here to be at least as new as the node's (re-run <span class="mono">fetch-qemu</span> after upgrading the cluster).</p>
</div></div>`;
}

// ------------------------------------------------------------- main loop
async function refresh() {
  const r = await api('tree');
  if (!r.ok) { $('#hdr-status').textContent = r.login ? '' : 'API error: ' + r.error; return; }
  S.tree = r; $('#hdr-status').textContent = 'updated ' + new Date().toLocaleTimeString();
  $('#ver').textContent = 'v' + (r.host.version || '');
  if (!S.sel) { const [id, sub] = decodeURIComponent(location.hash.slice(1)).split('|'); S.sel = id || 'dc'; S.sub = sub || null; }
  renderTree(); renderView(false);
}
$('#refresh').onclick = () => { refresh(); refreshJobs(); };
window.addEventListener('hashchange', () => { const [id, sub] = decodeURIComponent(location.hash.slice(1)).split('|'); if (id && (id !== S.sel || (sub || null) !== S.sub)) select(id, sub); });
(async () => {
  const w = await api('whoami');
  if (w.ok) { S.user = w.user; $('#user').textContent = w.user; refresh(); refreshJobs(); }
})();
setInterval(() => { if (!document.hidden && !S.loggingIn) refresh(); }, 6000);
setInterval(() => { if (!document.hidden && !S.loggingIn) refreshJobs(); }, 8000);
'''


if __name__ == "__main__":
    os.environ.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    if "/usr/sbin" not in os.environ["PATH"].split(":"):
        os.environ["PATH"] += ":/usr/local/sbin:/usr/sbin:/sbin"
    main()
