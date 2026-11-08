# proxmox-portable

Take a VM or container out of your Proxmox VE cluster, run it on a laptop (or any spare Debian
box), keep its changes flowing back to the cluster, and bring it home again, live.

`proxmox-portable` is **one Python script** that you run on that portable machine. It gives you a
Proxmox-style web console on port **8006**, runs the moved guests locally with **Proxmox's own
QEMU** and the exact command line Proxmox builds for them, and handles every move in both
directions. The machine never joins the cluster, so it can't affect quorum, and it can leave the
LAN while guests run on it.

- **VMs, live:** no reboot, a pause of about 0.02-0.3 s, both directions, over Wi-Fi too, and between
  Intel and AMD CPUs. Disks are pre-seeded while
  the VM runs, then only the blocks written since are mirrored, then RAM is migrated.
- **VMs, cold:** shut down, copy (checksum-verified; empty space is skipped), boot on the portable host.
- **Containers:** restart mode (rsync while it runs, short stop, final delta), 10-30 s downtime.
  Proxmox itself can't live-migrate containers either.
- **Same virtual hardware:** machine type, UEFI firmware and variables, TPM state, MACs, SMBIOS
  UUID and vmgenid are unchanged, so Windows activation, BitLocker and NIC names don't notice.
- **The cluster keeps a current copy.** The original guest stays on its node, stopped and tagged
  ("parked"). The portable host writes its changed blocks into it on a schedule, so Proxmox
  replication and scheduled backups keep covering it.
- **Keeps its IP.** In LAN mode the guest keeps its address, over Wi-Fi too. In NAT mode it runs
  behind the portable host anywhere.
- **Consoles in the browser:** noVNC for VMs and a terminal (xterm.js) for containers running on the
  portable host.
- **Return anywhere:** bring a guest back to its home node or to any other node.
- **raw and qcow2:** ZFS, LVM/LVM-thin and raw files, plus qcow2 images on directory/NFS storage.

![layout](https://img.shields.io/badge/UI-Proxmox--style-e57000) ![deps](https://img.shields.io/badge/dependencies-python3%20stdlib-blue)

---

## Contents

1. [How it works](#how-it-works)
2. [Requirements](#requirements)
3. [What has been tested](#what-has-been-tested)
4. [Install](#install)
5. [Prepare the Proxmox cluster](#prepare-the-proxmox-cluster)
6. [Configuration reference](#configuration-reference)
7. [Using it](#using-it)
8. [Use case: empty a node for maintenance or an upgrade](#use-case-empty-a-node-for-maintenance-or-an-upgrade)
9. [Networking](#networking)
10. [Security](#security)
11. [Limits](#limits)
12. [Files and logs](#files-and-logs)
13. [When something goes wrong](#when-something-goes-wrong)
14. [Updating](#updating)
15. [Uninstall](#uninstall)
16. [Releases](#releases)
17. [License](#license)

---

## How it works

```
             Proxmox VE cluster                                Portable host (Debian 13)
  ┌─────────────────────────────────────┐            ┌──────────────────────────────────────────┐
  │ node A                               │            │ proxmox-portable  (one script, as root)   │
  │   VM 101  (parked: stopped,          │  API 8006  │   ├─ web console  https://host:8006       │
  │            onboot=0,                 │◄───────────┤   ├─ scheduler    (graphs, syncs)         │
  │            tag portable-<host>)      │            │   └─ jobs         (move / sync / return)  │
  │                                      │  SSH 22    │                                           │
  │   raw disk  ◄── changed blocks ──────┼────────────┤ Proxmox QEMU  ──► VM 101 (running)        │
  │                 (sync, under the     │  (root)    │   disk = base.raw + qcow2 overlay          │
  │                  replication lock)   │            │ LXC          ──► CT 102 (running)          │
  └─────────────────────────────────────┘            └──────────────────────────────────────────┘
```

- **Console.** The script serves the web UI and a small JSON API on port 8006. It reads the cluster
  through the Proxmox API (an API token), so you see the Datacenter, nodes, guests, graphs,
  replication, backups and tasks, plus a branch for the portable host and the guests on it.
- **Running a VM here.** The script asks the node for `qm showcmd <vmid>`, the exact QEMU command
  line Proxmox would use, and runs it with Proxmox's own QEMU build. That build is copied from a
  node and unpacked into `/opt/proxmox-portable/pve-qemu`, beside Debian's packages, because the
  packages conflict. Only disk paths, tap scripts and passthrough devices are rewritten.
- **Live move.**
  1. A dirty bitmap is added on the source.
  2. Each disk is pre-seeded with a compressed copy while the VM keeps running.
  3. The local QEMU starts with `-incoming` and exports its disks over NBD, through an SSH tunnel
     that this host opens to the node.
  4. The node mirrors only the blocks written since the pre-seed, then migrates RAM.
  5. Auto-converge, a stepped downtime limit (300 ms up to 5 s) and a write-blocking mirror fallback
     make it finish over slow links.
- **Sync.** Each local disk is a raw *base* (identical to the parked disk) plus a qcow2 *overlay*
  holding every write since.
  1. A sync freezes the guest briefly through its guest agent, when it has one, and starts a new
     overlay.
  2. It ships the previous overlay to the node.
  3. It writes that overlay into the parked disk. During the write it holds Proxmox's replication
     lock and sets `lock: migrate` on the guest. Changed EFI vars and TPM state are written in the
     same locked window.
  4. It folds the overlay into the local base.

  Containers sync with rsync instead, in two passes; the second runs with the container frozen.
- **Return.**
  - *Live:* sync, then start a target QEMU on the home node on the parked disks, mirror only the
    blocks changed since that sync, migrate RAM back, and restore tags, onboot and HA.
  - *Cold:* shut down, final sync, optional full sha256 comparison, then start on the node.
  - *To another node:* after the final sync, Proxmox's own offline `qm migrate` / `pct migrate`
    moves the parked copy to the chosen node, and the return finishes there. Replication to that
    node makes this quick.
  - If a live step fails, the guest keeps running where it was.
- **Console.** The VM's QEMU VNC socket (password-protected, as on Proxmox) gets a fresh one-time
  password per session. The browser runs noVNC, and the script bridges its WebSocket to that
  socket itself, so websockify isn't needed. Containers get a terminal: xterm.js in the browser,
  bridged by the script to a PTY running `lxc-console` (the container's login prompt) or `lxc-attach`
  (a root shell), following the container's Proxmox console mode.
- **Copies.** Disk copies (cold moves, the live pre-seed) and the checksum check on a cold return
  run a small helper on the node. It reads the volume in 4 MiB chunks, sends an empty (all-zero)
  chunk as a 5-byte marker instead of its data, and computes the checksum as sha256 over the
  per-chunk sha256 sums on several threads. The same checksum is computed here. ZFS volumes don't
  report where their empty space is, so empty space is found by its content.
- **qcow2.** Cold copies and checksum checks read qcow2 images through Proxmox's own `qemu-nbd`
  (read-only, on a private socket) with a small NBD client sent over SSH. Live moves mirror qcow2
  disks in full; only raw disks get the pre-seed. Syncs write into qcow2 parked disks with
  `qemu-img commit`.
- **Long jobs** (move, sync, return) run as transient systemd units, so restarting the console
  never interrupts them. Moved VMs also run in their own units (`pportable-vm-<vmid>`).

---

## Requirements

### The portable host

| | Needed |
|---|---|
| OS | **Debian 13 (trixie)**, amd64. Other Debian-based systems may work, but are untested; the library package names in `setup` are trixie's. |
| CPU | x86-64 with hardware virtualisation; `/dev/kvm` must exist. |
| Python | 3.11 or newer, standard library only (Debian 13 ships 3.13). No pip packages. |
| Disk | Room for the guests' disks in `data_dir` (default `/var/lib/proxmox-portable`). Preflight asks for about 1.1 x the used size + 2 GiB. |
| Network | Must reach every Proxmox node on TCP 22 (SSH) and TCP 8006 (API). No inbound connection from the cluster is needed. |
| Privileges | Runs as root (it creates bridges, runs QEMU/LXC and writes firewall rules). |

**Debian packages.** `proxmox-portable setup` installs all of these:

| Package(s) | Why |
|---|---|
| `qemu-utils` | `qemu-img`: overlays, commits |
| `lxc`, `fuse3` | running containers |
| `rsync` | container copies and syncs |
| `zstd` | compressed disk streams |
| `dnsmasq-base` | DHCP for guests (LAN static leases, NAT pool) |
| `nftables`, `iptables`, `iproute2` | bridges, NAT, MAC rewrite rule, FORWARD accept |
| `openssh-client`, `openssl` | SSH to the nodes; the self-signed HTTPS certificate |
| `libaio1t64`, `libasound2t64`, `libcap-ng0`, `libcurl3t64-gnutls`, `libepoxy0`, `libfdt1`, `libfuse3-4`, `libgbm1`, `libglib2.0-0t64`, `libgnutls30t64`, `libiscsi7`, `libjpeg62-turbo`, `libnuma1`, `libpixman-1-0`, `libpng16-16t64`, `libpulse0`, `libseccomp2`, `libslirp0`, `libsndio7.0`, `libspice-server1`, `liburing2`, `libusb-1.0-0`, `libusbredirparser1t64`, `libvirglrenderer1`, `libxkbcommon0`, `libibverbs1`, `liblttng-ust1t64`, `librdmacm1t64`, `libnbd0`, `libcryptsetup12`, `libjson-glib-1.0-0` | shared libraries Proxmox's QEMU and swtpm build needs |

To install them by hand instead:

```bash
sudo apt-get install --no-install-recommends qemu-utils dnsmasq-base zstd python3 rsync nftables iptables lxc fuse3 openssl openssh-client iproute2 libaio1t64 libasound2t64 libcap-ng0 libcurl3t64-gnutls libepoxy0 libfdt1 libfuse3-4 libgbm1 libglib2.0-0t64 libgnutls30t64 libiscsi7 libjpeg62-turbo libnuma1 libpixman-1-0 libpng16-16t64 libpulse0 libseccomp2 libslirp0 libsndio7.0 libspice-server1 liburing2 libusb-1.0-0 libusbredirparser1t64 libvirglrenderer1 libxkbcommon0 libibverbs1 liblttng-ust1t64 librdmacm1t64 libnbd0 libcryptsetup12 libjson-glib-1.0-0
```

**Console files (noVNC and xterm.js).** `setup` runs `fetch-console`, which downloads two Debian
packages with `apt-get download` and unpacks only their static files. Neither package is
installed, so their dependencies are not pulled in; the WebSocket bridges are built into the
script. This needs working apt sources.

| Package (Debian 13) | Unpacked to | Size | Not needed because |
|---|---|---|---|
| `novnc` 1.6.0 | `/opt/proxmox-portable/novnc` | 1.6 MB | it depends on `nodejs` and `websockify`; the bridge is built in |
| `node-xterm` 5.3.0 (only `xterm` and `xterm-addon-fit`) | `/opt/proxmox-portable/xterm` | 2.6 MB | it only *suggests* `nodejs` |

From a node, `fetch-qemu` copies Proxmox's own packages, so the versions match your cluster:
`pve-qemu-kvm`, `pve-edk2-firmware-ovmf`, `pve-edk2-firmware-legacy`, `swtpm`, `swtpm-libs`,
`libtpms0`, `libproxmox-backup-qemu0`, `librados2`, `librbd1`. It unpacks them; it does not install
them as packages.

### The browser

Any current Chrome, Edge, Firefox or Safari. The consoles need WebSockets and JavaScript modules,
which every browser from the last several years has. If you put a reverse proxy in front of
port 8006, it must pass WebSocket upgrades for `/vncws` and `/termws`. Otherwise the consoles
won't connect, while the rest of the UI still works.

### The Proxmox cluster

- **Proxmox VE 9.x** (tested on 9.2). 8.x is likely to work but is untested.
- Guest disks on **raw** volumes (ZFS zvols, LVM / LVM-thin, raw files) or **qcow2** images
  (directory, NFS, CIFS storage). Other formats (vmdk, ...) are refused by the preflight check.
- Root SSH from the portable host to every node (one key, see below).
- An API token for the console (recommended), or none, in which case the console reads the cluster
  with `pvesh` over the same SSH connection (works, but each page loads in seconds, not
  milliseconds).
- `python3`, `zstd`, `rsync`, `flock` and `qemu-nbd` (part of `pve-qemu-kvm`) on the nodes. A
  standard Proxmox install has all of them.

---

## What has been tested

proxmox-portable **1.0.0**, **1.1.0** and **1.2.0**, 2026-11-08, against a 10-node cluster, from **two portable hosts**: an
Intel laptop on wired Ethernet and an AMD laptop on Wi-Fi. Throwaway guests were created for the
tests and destroyed afterwards. Each guest wrote checksummed files inside itself before and
during the moves, and they were re-verified after every step.

**Software versions**

| Component | Version |
|---|---|
| Portable host OS | Debian GNU/Linux 13.7 (trixie), kernel 6.12.115+deb13.1-amd64 |
| Portable host A | Intel Core i7-12700H laptop, wired Ethernet (USB adapter) |
| Portable host B | AMD Ryzen 5 3500U laptop, **Wi-Fi only**, Debian 13.7, same kernel and package versions |
| Python | 3.13.5 |
| LXC / dnsmasq / nftables / rsync / zstd / OpenSSH | 6.0.4 / 2.91 / 1.1.3 / 3.5.0 / 1.5.7 / 10.0p2 |
| Proxmox VE | proxmox-ve 9.2.0, pve-manager 9.2.26, kernel 7.0.14-pve |
| Proxmox QEMU (node and bundle) | pve-qemu-kvm 11.1.3-1, qemu-server 9.2.15 |
| swtpm / LXC on nodes / pve-container / ZFS | 0.10.2+pve1 / lxc-pve 7.0.0-2 / 6.1.18 / 2.4.4-pve1 |
| Source node CPU | Intel Core i7-10700 |
| Guest storage | ZFS (`zfspool`): raw zvols and subvolumes; **LVM-thin** (`lvmthin`) |

**Results**

| Test | Result |
|---|---|
| `setup`, `fetch-qemu`, `check` | ok; `fetch-qemu` took 3 s; `check` reached all 10 nodes |
| Web console: login, wrong password, missing CSRF header, session expiry on restart | ok (401 / 403 / login dialog as designed) |
| Console speed, API token mode | tree 0.5 s, node page 0.7 s, guest page 0.2 s |
| Console speed, no token (`pvesh` over SSH) | works, about 3 s per page |
| **Live VM move** (BIOS, i440fx, 20 GiB disk + cloud-init, LAN mode), started from the web UI | pre-seed at 162 MiB/s, **90 ms downtime**, guest not rebooted, all files intact, same IP |
| Manual sync while running | 205 MiB of changed blocks in 12 s, guest-agent quiesce |
| **Live VM return** | **98 ms downtime**, guest not rebooted, 35/35 files intact, tags and onboot restored |
| **Cold VM move** (LAN mode, DHCP static lease) | 20 GiB at 161 MiB/s, sha256 verified, booted here, got its own IP back |
| **Cold VM return** with full sha256 verify | parked disk identical to the local copy, started on the node |
| **Container move** (unprivileged Debian 13, nesting, LAN mode) | pre-copy 571 MiB in 7 s, about 16 s downtime, data intact, same IP |
| **Container return** | two-pass sync (running, then frozen), started on the node, 6/6 files intact |
| **Live UEFI + TPM 2.0 VM move** (OVMF, q35, NAT mode) | **21 ms downtime** |
| Scheduled sync (15 min interval, built-in scheduler) | started on its own, EFI vars and TPM state written back |
| **Live UEFI + TPM return** | 95 ms downtime |
| Uninstall steps below | ok, nothing left behind |

**Host B (AMD, Wi-Fi):**

| Test | Result |
|---|---|
| **Live VM move Intel -> AMD over Wi-Fi**, guest **HA-managed**, writing a 5 MB file every second throughout | pre-seed 20 GiB at 109 MiB/s, **274 ms downtime**, no reboot, all files and in-flight writes intact; HA entry removed while away |
| **Live VM return AMD -> Intel over Wi-Fi**, still under write load | **299 ms downtime**; all 400 writes made during both moves verified; HA re-added with its `max_restart` and comment, state `started` |
| **Cold VM move from LVM-thin** with `usb0: host=0bda:0129` (by vendor:product) and `usb1: host=1-2` (by port) | 20 GiB at 105 MiB/s over Wi-Fi, sha256 verified; the USB device (plugged into host B) attached inside the guest; the port-based entry was dropped |
| **Cold VM return to LVM-thin** with full sha256 verify | parked LVM volume identical to host B's copy; both USB entries unchanged in the Proxmox config; a file written on host B arrived |
| Preflight on LVM-thin | counts only the allocated part (1.3 of 20 GiB) after the fix in this release |

**Added in 1.1.0** (host A, same cluster and versions, throwaway guests, checksummed data):

| Test | Result |
|---|---|
| `fetch-novnc` | noVNC 1.6.0 unpacked from Debian's package; no extra packages installed |
| **VNC console**, HTTP and HTTPS (`wss://`) | connected, showed the VM's screen; key presses reached the VM (`echo hi vnc` ran) |
| Console security | needs a login (redirect / 401); WebSocket from a foreign `Origin` refused (403); `/novnc/../` path traversal blocked (404) |
| **qcow2 cold move** (disk and cloud-init drive qcow2, on directory storage) | read through `qemu-nbd`, **257 MiB/s**, sha256 verified, booted here, data intact |
| **qcow2 cold return** with sha256 verify | synced into the qcow2 parked disk; checksum identical; `qemu-img check` clean |
| **qcow2 live move / return** (full mirror, no pre-seed) | **148 ms / 123 ms** downtime, data intact |
| **Live VM return to a different node** (home node A → node B) | parked copy moved by `qm migrate` in 18 s, then **66 ms** downtime onto node B; data intact |
| **Cold VM return to a different node** (home node B → node A) with verify | checksum taken on the moved disk: identical; started on node A |
| **Container return to a different node** (home node A → node C) | two-pass sync, `pct migrate`, started on node C, data intact |
| Return to a node lacking the guest's storage | refused **before** anything stopped, with a clear message |

**Added in 1.2.0** (host A, same cluster and versions, throwaway guests, checksummed data):

| Test | Result |
|---|---|
| `fetch-console` | noVNC 1.6.0 and xterm.js 5.3.0 unpacked from Debian's packages; nothing installed |
| **Cold VM move**, 20 GiB thin ZFS volume holding 1.4 GiB | **37 s (551 MiB/s)**, only the data sent, checksum verified (1.0.0: 2 min 7 s at 161 MiB/s) |
| **Live VM move**, same disk | pre-seed **37 s** (1.1.0: 2 min 6 s), **82 ms** downtime; live return **97 ms** |
| Checksum check on a cold return, same disk | **25 s** (1.1.0: about 95 s), identical |
| Benchmark, 8 GiB thin volume with 1.2 GiB data | copy 17 s, checksum check 10 s (previous method: 33 s and 30 s); the local file allocates only the 1.2 GiB of data |
| qcow2 through the new copy | cold move and return: identical checksums, only the data sent |
| **Container console**, mode `tty` (default) | the container's own login prompt (`pp-test-ct login:`); typing reached it |
| **Container console**, mode `shell` | root shell, clean environment; the terminal size followed the browser window (`stty size` 45 119) |
| Leaving a console page / restarting the service | the `lxc-console` / `lxc-attach` process is ended; nothing is left running |
| VM console after the WebSocket rework | still works |

Not tested:
- thick (non-thin) LVM
- Proxmox VE 8.x
- other Debian versions on the portable host
- the VM console's "Type clipboard" button (it sends Shift for capitals and symbols on a US layout)
- the container console's `console` mode (`/dev/console`)
- the node-upgrade use case below

---

## Install

Everything below runs **on the portable host**, as root (or with `sudo`).

### 1. Get the script

```bash
sudo apt-get install -y git
git clone https://github.com/SixOfFive/proxmoxPortable.git
cd proxmoxPortable
sudo install -m 0755 proxmox-portable.py /usr/local/sbin/proxmox-portable
```

The script is the whole program. Later updates replace just this one file.

### 2. Create the config file

```bash
sudo install -d -m 0700 /etc/proxmox-portable
sudo install -m 0600 proxmox-portable.conf.example /etc/proxmox-portable/proxmox-portable.conf
sudo nano /etc/proxmox-portable/proxmox-portable.conf
```

At minimum, change:

| Setting | Set it to |
|---|---|
| `[web] password` | your console password (or use `password_hash`, below) |
| `[proxmox] hosts` | two or three of your nodes' IPs |
| `[proxmox] token` | the API token from [step 2 of the cluster preparation](#2-create-an-api-token-for-the-console) (fill it in after you create it) |
| `[network] gateway`, `prefix`, `dns` | your LAN's gateway, prefix length and DNS servers |

The service refuses to start while the password is still `change-me`. To store a hash instead
of the plain password:

```bash
sudo proxmox-portable hash-password
```

Paste the printed `password_hash = ...` line into `[web]`.

### 3. Run setup

```bash
sudo proxmox-portable setup
```

This does the following:

- installs the Debian packages listed above
- creates `/var/lib/proxmox-portable`
- adds `root:100000:65536` to `/etc/subuid` and `/etc/subgid`, which unprivileged containers need
- generates the SSH key `/etc/proxmox-portable/id_pve`
- writes and enables `proxmox-portable.service`
- unpacks the web consoles' files (`fetch-console`, see above)

At the end it prints a public key line. Keep it for the next part.

### 4. Prepare the cluster

Follow [Prepare the Proxmox cluster](#prepare-the-proxmox-cluster) below: add the key, then create
the token and put it into the config file.

### 5. Copy Proxmox's QEMU onto the host

```bash
sudo proxmox-portable fetch-qemu              # uses the first node it finds
sudo proxmox-portable fetch-qemu 192.168.1.11 # or name a node
```

This downloads `pve-qemu-kvm`, the OVMF firmware and swtpm on a node, so the versions match the
cluster. It unpacks them into `/opt/proxmox-portable/pve-qemu` and points `/usr/share/kvm` and
`/usr/share/pve-edk2-firmware` there, because Proxmox's command lines name those paths. Debian's
own QEMU packages, if you have any, are not touched.

### 6. Check everything

```bash
sudo proxmox-portable check
```

Every line should say `ok`: the config, `/dev/kvm`, the API, root SSH to each node, noVNC and xterm.js, the QEMU
bundle, the tools, and the LAN gateway and interface.

### 7. Start it

```bash
sudo systemctl start proxmox-portable
sudo journalctl -u proxmox-portable -f        # optional: watch it start
```

Open **`https://<portable-host>:8006/`** in a browser. Like Proxmox, it uses a self-signed
certificate on first start, so accept the browser's warning once. Then log in with the username
and password from the config file.

---

## Prepare the Proxmox cluster

Do this once, on **any one node**, as root. `/etc/pve` is shared, so it applies to the whole
cluster.

### 1. Let the portable host in over SSH

Add the public key that `setup` printed (it is also in `/etc/proxmox-portable/id_pve.pub` on the
portable host):

```bash
echo 'ssh-ed25519 AAAA...your-key... proxmox-portable@yourhost' >> /etc/pve/priv/authorized_keys
```

Check from the portable host: `sudo proxmox-portable check` should show `ok root SSH to <node>`
for every node.

Why root SSH: the moves need commands that only root on a node can run. They read raw volumes,
talk to the source QEMU's QMP socket, run `qm showcmd`, hold `/var/lock/pvesr.lck` and write
synced blocks into the parked disks.

### 2. Create an API token for the console

The console reads the cluster through the API. The tested setup is a `root@pam` token without
privilege separation.

**In the web UI:**

1. Go to *Datacenter > Permissions > API Tokens > Add*.
2. Set User to `root@pam` and Token ID to `portable`.
3. Untick **Privilege Separation**.
4. Copy the secret it shows (it is shown only once).

**Or on the command line:**

```bash
pveum user token add root@pam portable --privsep 0
```

Put it into the config as `token = root@pam!portable=<secret>`, then restart the service.

**Least privilege (optional, untested).** The console needs to read the cluster and to start,
stop, shut down and reboot guests. A dedicated user with privilege separation should work:

```bash
pveum role add PortableConsole --privs "Sys.Audit,VM.Audit,VM.PowerMgmt,Datastore.Audit,VM.GuestAgent.Audit"
pveum user add portable@pve
pveum aclmod / --users portable@pve --roles PortableConsole
pveum user token add portable@pve console --privsep 0
```

Note that the SSH key from step 1 already gives this host root on the nodes, so a narrower token
limits only what the browser session can do.

### 3. Firewall (only if you use the Proxmox firewall)

If the datacenter firewall is on, allow the portable host's IP to reach the nodes on TCP 22 and
TCP 8006. The cluster never connects back to the portable host, because every tunnel is opened
from the portable host.

### 4. Nothing else

No packages, no cluster membership and no storage changes are needed on the nodes. While a guest
is on the portable host, the only change on the cluster side is to that guest's config:

- the tag `portable-<host>`
- `onboot: 0`
- removal from HA, if it was HA-managed

All three are restored on return.

---

## Configuration reference

File: `/etc/proxmox-portable/proxmox-portable.conf`. Use another path with
`proxmox-portable --config FILE ...` or the `PROXMOX_PORTABLE_CONF` environment variable. Put
comments on their own lines: a `#` after a value counts as part of the value. Restart the service
after editing.

| Section | Key | Default | Meaning |
|---|---|---|---|
| `[web]` | `listen` | `0.0.0.0` | address the console listens on (`0.0.0.0` = the whole LAN; `::` for IPv6) |
| | `port` | `8006` | console port |
| | `username` | `admin` | console login name |
| | `password` | (none) | console password, plain text; ignored when `password_hash` is set |
| | `password_hash` | (none) | `pbkdf2_sha256$...` from `proxmox-portable hash-password` |
| | `tls` | `yes` | HTTPS. `no` serves plain HTTP; only do that behind a TLS proxy or on localhost |
| | `tls_cert`, `tls_key` | `/etc/proxmox-portable/tls.crt`, `.key` | made self-signed on first start if missing; point them at a real certificate if you have one |
| | `session_hours` | `12` | how long a login lasts. Sessions are kept in memory, so a restart logs everyone out |
| | `allowed_networks` | (empty = all) | comma-separated CIDRs that may open the console; localhost is always allowed |
| `[proxmox]` | `hosts` | (required) | node IPs or names; the first that answers is used |
| | `token` | (empty) | `user@realm!tokenid=secret`; empty = read the cluster with `pvesh` over SSH |
| | `verify_tls` | `no` | check the nodes' HTTPS certificates |
| | `ssh_key` | `/etc/proxmox-portable/id_pve` | key for root SSH to the nodes |
| | `ssh_known_hosts` | `/etc/proxmox-portable/pve_known_hosts` | nodes' host keys, learned on first contact |
| | `lan_bridges` | `vmbr0` | node bridges that are "the LAN"; NICs on them can keep their IP here |
| `[host]` | `name` | the hostname | this host's name in the cluster tag |
| | `tag_prefix` | `portable-` | parked guests get the tag `<tag_prefix><name>` |
| `[network]` | `gateway` | default route | the LAN gateway for LAN-mode guests |
| | `prefix` | `24` | the LAN's prefix length |
| | `dns` | the gateway | DNS servers handed to LAN-mode guests |
| | `lan_iface` | default route's interface | interface facing that LAN; Wi-Fi works |
| | `bridge` | `ppbr0` | routed bridge this script creates for LAN-mode guests |
| | `nat_bridge` | `ppnat0` | NAT bridge this script creates |
| | `nat_network` | `10.77.0.1/24` | this host's address on the NAT bridge; DHCP hands out the upper half |
| `[paths]` | `data_dir` | `/var/lib/proxmox-portable` | guests' disks, job logs, graph samples |
| | `qemu_bundle` | `/opt/proxmox-portable/pve-qemu` | where `fetch-qemu` unpacks Proxmox's QEMU |
| | `novnc_dir` | `/opt/proxmox-portable/novnc` | where `fetch-console` unpacks noVNC (VM console) |
| | `xterm_dir` | `/opt/proxmox-portable/xterm` | where `fetch-console` unpacks xterm.js (container console) |
| `[defaults]` | `shutdown_timeout` | `180` | seconds to wait for a clean shutdown before a cold return |
| | `sync_interval` | `hourly` | default for new moves: `15min`, `30min`, `hourly`, `2h`, `6h`, `daily`, `off` |

Several portable hosts can share one cluster. Give each its own `[host] name`. Each one manages
only the guests carrying its own tag.

---

## Using it

### In the console

The tree looks like Proxmox's. *Datacenter* lists the nodes and their guests, and below it is
**`<host> (portable)`** with the guests running here.

| To | Do |
|---|---|
| **Move a guest here** | Select it, then **Move to \<host\>** (or the *Move here* tab). The preflight check runs automatically: free space, CPU count, raw volumes, passthrough, pending config changes and whether a live move is possible. Choose live or cold, LAN or NAT, the sync interval, the timeout and whether to start it. Confirm by typing the VMID. |
| Watch a job | The job window opens by itself. Jobs are also in the bottom panel (*Portable jobs*) and in *Jobs* on the host and guest pages. |
| **Sync now / change the schedule** | Guest on the host > *Sync & Backup* |
| Start / stop a guest here | the toolbar on the guest's page |
| **Open a console** | Guest on the host > **Console**. For a VM: the screen, Ctrl+Alt+Del, *Type clipboard* (types text as key presses, US layout), full screen and reconnect. For a container: a terminal. What it shows follows the container's Proxmox *Console mode* (`cmode`): `tty` (default) is its login prompt, `shell` is a root shell, `console` is `/dev/console`. Consoles close when the service restarts; ⟳ reconnects. |
| **Return it** | Guest on the host > **Return to Proxmox**: live or cold, **which node** (home or another), start there or not, sha256 verify (cold), delete the local copy or keep it |
| See replication/backups covering it | the guest's *Replication* / *Backup* tabs, or *Sync & Backup* |

Scheduled syncs run while the `proxmox-portable` service runs. If the service is stopped, they
resume when it starts again.

### On the command line

Every command prints JSON. Run them as root.

```bash
proxmox-portable status                 # this host: CPU, memory, guests
proxmox-portable list                   # guests managed here
proxmox-portable vm-status 101          # one guest, with its disk chain
proxmox-portable jobs                   # recent jobs
proxmox-portable job <job-id>           # one job with its log
proxmox-portable start|shutdown|stop|reboot 101
proxmox-portable sync 101               # start a sync job now
proxmox-portable set-sync 101 30min     # 15min 30min hourly 2h 6h daily off
proxmox-portable check                  # health check
proxmox-portable fetch-qemu [NODE_IP]   # refresh the Proxmox QEMU bundle
proxmox-portable fetch-console          # (re)unpack noVNC and xterm.js for the web consoles
```

Moves and returns are started from the console.

### What happens to replication, backups and HA

- **Replication (`pvesr`)** jobs keep running from the home node. They copy each synced state to
  the replica nodes. The sync holds `pvesr`'s lock while it writes, so a replication never
  snapshots a half-written disk.
- **Scheduled backups** back up the parked copy like any other stopped guest, as of its last sync.
  During a sync's write the guest carries `lock: migrate`, and a backup starting at that moment
  skips it once. Keep sync times away from backup windows.
- **HA:** the guest is removed from HA while it is away, because HA would otherwise start the
  parked copy. On return it is re-added with the same group, max_restart, max_relocate and
  comment.

Recovery points while a guest is away, newest first:

1. the portable host's copy
2. the parked copy (as of the last sync)
3. its replicas (as of the replication after that sync)
4. backups

---

## Use case: empty a node for maintenance or an upgrade

Have a node to reboot or upgrade, and nowhere else to put a guest that can't stop? Park it on the
portable host while you work on the node, then bring it back.

> **Untested.** This combines features that were tested on their own (live move out, live and cold
> return), but the whole sequence across a real Proxmox upgrade has **not** been tested. Neither
> has any other Proxmox VE version than the one in the table above, newer or older. Try it on a
> throwaway guest first.

1. **Before the upgrade**, make sure this host's QEMU matches the node: `sudo proxmox-portable
   fetch-qemu`.
2. **Move the guest here live** from the console. Set the sync interval to `off` for the
   duration. Syncs need the node, so they would fail while it reboots. A failed sync loses
   nothing; it just fails.
3. **Do the maintenance or upgrade** on the node and reboot it. Leave the parked guest alone: it
   must stay stopped, and the tag must stay.
4. **Bring it back:**
   - **Live return (try first).** This moves the guest from this host's older QEMU to the node's
     newer one. That is the same direction Proxmox's own rolling upgrades rely on, and QEMU keeps
     the guest on its old machine type (e.g. `pc-q35-11.1+pve0`), which newer QEMU versions still
     run. It should usually work. If the target refuses, the job stops the half-started target and
     the guest keeps running here.
   - **Cold return (always works).** Shut down here, final sync, optional sha256 verify, then it
     starts on the upgraded node. It is a reboot, but there is no version dependency.
5. Afterwards, run `sudo proxmox-portable fetch-qemu` again, so the next live move *out* of the
   upgraded node works. A live move needs this host's QEMU to be at least as new as the source's.

Caveats:
- Since 1.1.0 you can also return it to a different, already upgraded node instead (see
  [Using it](#using-it)).
- A major upgrade (for example 8 -> 9) changes much more than QEMU. Prefer a cold return there.
- In LAN mode the host must stay on the guest's LAN for the whole time.

---

## Networking

| Mode | What the guest gets | Works when |
|---|---|---|
| **LAN** (keep IP) | its old IP, gateway and DNS. This host routes a /32 to the bridge `ppbr0`, answers ARP for the guest on the LAN (proxy ARP), hands it its address by DHCP (static lease by MAC), rewrites guest frames to the bridge MAC (so a live-moved guest with the router's MAC cached keeps working) and sends gratuitous ARP on arrival | this host is on the guest's LAN, wired or Wi-Fi |
| **NAT** | a new address from `nat_network` (default 10.77.0.x) by DHCP, with DNS from this host, masqueraded out | always, including away from home. The guest must use DHCP. A container with a static IP in its Proxmox config keeps that static IP and has no connectivity in NAT mode. |

A guest that gets its address by DHCP **without a reservation** keeps it while it runs here (this
host hands out a static lease). But a cold move or cold return is a reboot, and after a reboot
the LAN's DHCP server may give it a different address, as with any reboot. Give such guests a
DHCP reservation or a static IP if their address matters.

Only guest NICs on one of the `lan_bridges` (default `vmbr0`) can use LAN mode. NICs on other
node bridges go to the NAT bridge.

---

## Security

- **This host is as powerful as a node.** Its root SSH key works on every node, and the config
  file holds the API token. Treat the portable host like a cluster node: use full-disk encryption
  on a laptop, and keep `/etc/proxmox-portable` at `0700` and the config at `0600` (`setup` sets
  both).
- **The console.**
  - One login.
  - HTTPS with TLS 1.2 or newer.
  - The session cookie is `HttpOnly`, `SameSite=Strict` and `Secure`.
  - POST requests need a custom header, which blocks cross-site requests.
  - After 5 failed logins from one address within 5 minutes, that address is refused for a while.
  - Strict security headers, including CSP and `X-Frame-Options`.
  - The VNC console needs a logged-in session. Its WebSocket is accepted only from the console's
    own origin. Each console session sets a new one-time VNC password on the VM.
  - The container console uses the same session and origin checks. In `shell` mode it is a root
    shell inside the container, so anyone who can log in to the console can administer the
    containers here. Use the default `tty` mode if you want the container's own login.
- **Exposure.** Serve it to your LAN only. Narrow it further with `allowed_networks`. Do not
  expose port 8006 to the internet.
- **To keep the config file out of version control,** the repository's `.gitignore` already
  excludes `*.conf`, keys, certificates and token files.

---

## Limits

- Volumes: raw (ZFS, LVM-thin, LVM, raw files) and qcow2. Tested: ZFS, LVM-thin, qcow2 on directory
  storage. Live moves of qcow2 disks mirror the whole allocated disk (no pre-seed), so over a
  slow link they take longer than raw ones.
- **Live moves need:**
  - the VM running
  - no passthrough devices
  - no pending config changes (restart the VM on Proxmox first)
  - this host's Proxmox QEMU at least as new as the node's (re-run `fetch-qemu` after cluster
    upgrades)
  - an explicit "accept CPU risk" for CPU type `host`/`max`. A named model such as `x86-64-v2-AES`
    is safe across CPUs.
- **Containers:** restart mode only. Host bind mounts (`mpN` with a host path) and `devN`
  passthrough are not carried; raw `lxc.*` keys are not carried.
- **Passthrough on a cold move:**
  - USB by vendor:product is kept. It attaches if that device is plugged into this host.
  - PCI, port-based USB, mapped USB and host serial/parallel ports are dropped here and are back
    on return.
- **Proxmox's own UI doesn't know about this.** Never start a `portable-*` tagged guest there.
  This console blocks that, and the portable host refuses to start or sync a guest whose Proxmox
  copy is running.
- **Returning to another node** needs the guest's storages on that node; the console checks this
  before anything stops. Without replication, the parked disks are copied by `qm migrate` /
  `pct migrate`, which takes as long as that copy.

---

## Files and logs

| What | Where |
|---|---|
| The program | `/usr/local/sbin/proxmox-portable` |
| Config, SSH key, known hosts, TLS cert | `/etc/proxmox-portable/` |
| Service | `proxmox-portable.service` (`journalctl -u proxmox-portable`) |
| One guest's state | `/var/lib/proxmox-portable/vms/<vmid>/meta.json` |
| A VM's disks | `.../vms/<vmid>/<disk>.base.raw` (= parked disk as of the last sync) and `<disk>.ov-*.qcow2` (writes since) |
| EFI vars / TPM state | `.../vms/<vmid>/efivars.raw`, `tpmstate.raw` |
| The exact QEMU command line used | `.../vms/<vmid>/cmdline` |
| A container's root filesystem | `.../vms/<vmid>/rootfs/` (LXC config in `/var/lib/lxc/pve-<vmid>/config`) |
| Jobs | `/var/lib/proxmox-portable/jobs/<id>.json` and `.log`, units `pportable-job-*` |
| A VM's QEMU output | `journalctl -u pportable-vm-<vmid>` |
| Graph samples (24 h) | `/var/lib/proxmox-portable/rrd/` |
| LAN-mode DHCP log | `/var/log/proxmox-portable-dnsmasq.log` |
| Proxmox QEMU bundle | `/opt/proxmox-portable/pve-qemu/` |
| noVNC (VM console) | `/opt/proxmox-portable/novnc/` (Debian's copyright file is in `COPYRIGHT.debian`) |
| xterm.js (container console) | `/opt/proxmox-portable/xterm/` (Debian's copyright file is in `COPYRIGHT.debian`) |
| Temporary sync files on a node | `/var/tmp/proxmox-portable/<vmid>/` |

---

## When something goes wrong

| Situation | What to do |
|---|---|
| A move failed before the guest booted here | The job already restored tags, onboot and HA, and restarted the guest on Proxmox if it was running. Read the job log. |
| A sync failed | Nothing is lost. Unsent overlays stay here and go out with the next sync; re-applying one is harmless. A leftover `lock: migrate` is cleared with `qm unlock <vmid>` on the node. |
| A live move or return doesn't converge (the guest writes faster than the link) | It raises the allowed downtime step by step up to 5 s. After about 45 s without progress it switches the disk mirror to write-blocking. To give up, run `migrate_cancel` on the source QEMU; the job cleans up and the guest keeps running where it was. |
| The portable host died | The parked copy is as of the last sync. On its node: `qm set <vmid> --onboot 1 --tags <tags without portable-*>`, then `qm start <vmid>` (`pct` for a container). Removing the tag is what tells the portable host to keep its hands off: if it comes back, it refuses to start or sync that guest. Then delete `/var/lib/proxmox-portable/vms/<vmid>` there. |
| Someone started the parked copy in Proxmox | The portable host refuses to sync into it or start its own copy. Decide which copy is current, shut the other one down, then either return it or move it again. |
| A guest here gets no IP in LAN mode | Check `/var/log/proxmox-portable-dnsmasq.log` and `/run/proxmox-portable/dnsmasq-hosts`, and that the host is on the guest's LAN. |
| `check` says a node's SSH fails | The key isn't in `/etc/pve/priv/authorized_keys`, or a firewall blocks TCP 22. |
| The console says the cluster is unreachable | Check `[proxmox] hosts` and the token, and run `proxmox-portable check`. |

**A cold return by hand:**

```bash
# on the portable host
proxmox-portable shutdown <vmid>        # wait until `proxmox-portable vm-status <vmid>` says stopped
proxmox-portable sync <vmid>            # final sync; wait for `proxmox-portable jobs` to show it ok
# on the home node (pct instead of qm for a container)
qm set <vmid> --onboot <old> --tags "<old tags without portable-*>"
ha-manager add vm:<vmid> --state started    # only if it was HA-managed
qm start <vmid>
# back on the portable host
rm -rf /var/lib/proxmox-portable/vms/<vmid>
```

---

## Updating

```bash
cd proxmoxPortable && git pull
sudo install -m 0755 proxmox-portable.py /usr/local/sbin/proxmox-portable
sudo systemctl restart proxmox-portable          # running guests and jobs keep running
```

**Upgrading from 1.0.x:** the web consoles (added in 1.1.0 and 1.2.0) need files that `setup`
only fetches on new installs. Run this once after updating, then check that `check` shows noVNC and
xterm.js as `ok`:

```bash
sudo proxmox-portable fetch-console
sudo proxmox-portable check
```

The Console buttons stay greyed out until then. Running `setup` again also works, and is safe to
repeat.

**New config keys** (`novnc_dir`, `xterm_dir` in `[paths]`) have defaults. Existing config files
don't need editing.

**After every Proxmox upgrade that brings a new `pve-qemu-kvm`, run
`sudo proxmox-portable fetch-qemu` again.** Live moves to this host need its QEMU to be at least
as new as the node's. Cold moves and returns don't depend on it.

---

## Uninstall

Return every guest to Proxmox first, from the console. Then check that no guest still carries a
`portable-<host>` tag. On any node:

```bash
pvesh get /cluster/resources --type vm --output-format json | grep -o '"tags":"[^"]*portable-[^"]*"'
```

**On the portable host, as root:**

```bash
systemctl disable --now proxmox-portable
rm -f /etc/systemd/system/proxmox-portable.service && systemctl daemon-reload
for f in /run/proxmox-portable/dnsmasq-*.pid; do kill "$(cat "$f")" 2>/dev/null; done
nft delete table bridge proxmox_portable; nft delete table ip proxmox_portable_nat
for br in ppbr0 ppnat0; do iptables -D FORWARD -i $br -j ACCEPT; iptables -D FORWARD -o $br -j ACCEPT; ip link del $br; done
rm -f /usr/share/kvm /usr/share/pve-edk2-firmware        # symlinks into the bundle
rm -rf /opt/proxmox-portable /var/lib/proxmox-portable /run/proxmox-portable /etc/proxmox-portable
rm -f /usr/local/sbin/proxmox-portable /var/log/proxmox-portable-dnsmasq.log
ls /var/lib/lxc/ | grep '^pve-'                           # delete any leftover pve-<vmid> containers
```

`/usr/share/qemu-server` was copied from a node; delete it too if nothing else uses it. The
`root:100000:65536` lines in `/etc/subuid` and `/etc/subgid` are harmless, and LXC may use them
anyway. The Debian packages can stay.

**On the cluster (any node):** remove the key, the token and any leftover sync files.

```bash
sed -i '/ proxmox-portable@<host>$/d' /etc/pve/priv/authorized_keys
pveum user token remove root@pam portable
rm -rf /var/tmp/proxmox-portable                          # on each node that received syncs
```

---

## Releases

Each release is a git tag. A tag's page has the source as `.zip` / `.tar.gz`. `main` may hold
newer, not yet released work.

| Version | Date | What | Get it |
|---|---|---|---|
| **v1.2.0** | 2026-11-08 | Container console (xterm.js from Debian, PTY bridge built in, follows the container's console mode); disk copies skip empty space and use a parallel chunked checksum: cold copies and pre-seeds about 3.4x faster, the cold-return check about 4x faster | [tag v1.2.0](https://github.com/SixOfFive/proxmoxPortable/releases/tag/v1.2.0) · [script only](https://raw.githubusercontent.com/SixOfFive/proxmoxPortable/v1.2.0/proxmox-portable.py) · [.tar.gz](https://github.com/SixOfFive/proxmoxPortable/archive/refs/tags/v1.2.0.tar.gz) |
| **v1.1.0** | 2026-11-08 | VNC console for VMs on the portable host (noVNC unpacked from Debian, WebSocket bridge built in); qcow2 source volumes; return to a different node; target-storage check; faster qcow2 reads | [tag v1.1.0](https://github.com/SixOfFive/proxmoxPortable/releases/tag/v1.1.0) · [script only](https://raw.githubusercontent.com/SixOfFive/proxmoxPortable/v1.1.0/proxmox-portable.py) · [.tar.gz](https://github.com/SixOfFive/proxmoxPortable/archive/refs/tags/v1.1.0.tar.gz) |
| **v1.0.0** | 2026-11-08 | First public release. Live/cold VM moves, container moves, sync, return; tested as described in [What has been tested](#what-has-been-tested) | [tag v1.0.0](https://github.com/SixOfFive/proxmoxPortable/releases/tag/v1.0.0) · [script only](https://raw.githubusercontent.com/SixOfFive/proxmoxPortable/v1.0.0/proxmox-portable.py) · [.tar.gz](https://github.com/SixOfFive/proxmoxPortable/archive/refs/tags/v1.0.0.tar.gz) |

**Next: v1.3.0** (in development on `main`). Planned:
- a real test of the node-upgrade use case, as soon as Proxmox ships a newer `pve-qemu-kvm` (the
  cluster this was tested on had nothing newer to upgrade to)
- testing the gaps listed under [What has been tested](#what-has-been-tested): thick LVM, Proxmox
  VE 8.x, the container console's `console` mode

---

## License

MIT. See [LICENSE](LICENSE). Use at your own risk: this tool moves live guests and writes into
their disks, so test it on throwaway guests first.
