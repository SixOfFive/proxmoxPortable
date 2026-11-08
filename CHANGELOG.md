# Changelog

All notable changes to proxmox-portable. Newest first.

## Unreleased

### Changed
- README: a "The browser" requirement (WebSockets and JS modules; a reverse proxy must pass
  WebSocket upgrades for `/vncws` and `/termws`). "Updating" now says that installs from 1.0.x must
  run `fetch-console` once for the consoles, and that the new config keys have defaults.

## 1.2.0 - 2026-11-08 - released as tag `v1.2.0`

### Added
- **Container console**: the *Console* button now works for containers too. It opens an xterm.js
  terminal bridged by the script to a PTY on the portable host. What it runs follows the
  container's Proxmox console mode:
  - `tty` (default): `lxc-console`, the container's login prompt
  - `console`: `/dev/console`
  - `shell`: `lxc-attach` with a clean environment, a root shell

  Window resizes are passed on (`TIOCSWINSZ`). The PTY process is ended when the page closes or
  the service restarts.
- `fetch-console` (`fetch-novnc` still works) unpacks noVNC **and** xterm.js 5.3 from Debian's
  `novnc` / `node-xterm` packages without installing them. `xterm_dir` is in `[paths]`, and
  `check` reports xterm.js.

### Changed
- **Disk copies skip empty space and check faster.** A helper on the node reads the volume in
  4 MiB chunks:
  - all-zero chunks travel as a 5-byte marker instead of 4 MiB
  - the checksum is sha256 over the per-chunk sha256 sums, on 4 threads on both ends (one
    `sha256sum` was the bottleneck)
  - the same format serves cold copies, the live pre-seed, qcow2 (via the NBD reader) and the
    checksum check on a cold return

  Measured on a 20 GiB thin ZFS volume holding 1.4 GiB:
  - cold copy and pre-seed: 2 min 7 s -> 37 s
  - cold-return check: about 95 s -> 25 s

  ZFS volumes don't report holes (`SEEK_DATA` fails; `qemu-img map` says all data), so empty
  space is detected by content.
- The WebSocket code is shared by the VM and container consoles.

### Not done
- The node-upgrade use case is still untested: the test cluster had no newer `pve-qemu-kvm`
  to upgrade to.

## 1.1.0 - 2026-11-08 - released as tag `v1.1.0`

### Added
- **VNC console** for VMs running on the portable host: a *Console* button on the guest's toolbar
  opens a window with the screen, Ctrl+Alt+Del, *Type clipboard* (US layout, Shift sent for
  capitals/symbols), full screen and reconnect.
  - noVNC 1.6 comes from Debian's `novnc` package via the new `fetch-novnc` (also run by `setup`):
    `apt-get download` + `dpkg -x`, so its dependencies (`nodejs`, `websockify`) are not
    installed.
  - The WebSocket-to-VNC bridge is built into the script, stdlib only.
  - Each session sets a one-time VNC password through QMP; the VM's VNC socket stays
    password-protected, as Proxmox sets it.
  - Security: a session is required, the WebSocket `Origin` must match the console host, the CSP
    allows WebSockets only to that host, and static files are served path-checked.
- **qcow2 source volumes** (directory/NFS/CIFS storage):
  - Cold copies and the return checksum read the image through Proxmox's own `qemu-nbd`
    (read-only, private socket) with a small pipelined NBD client sent over SSH.
  - Live moves mirror qcow2 disks in full (no pre-seed).
  - Syncs commit into qcow2 parked disks; EFI vars on qcow2 are written with
    `qemu-img convert -n`.
  - Sizes come from `qemu-img info -U` (virtual / actual size).
- **Return to a different node**: *Return to node* in the return form. After the final sync,
  Proxmox's offline `qm migrate` / `pct migrate` moves the parked copy, and the return (live or
  cold, with verify) finishes there. The target's storages are checked before anything stops.
- `novnc_dir` in `[paths]`; `check` reports noVNC.

### Changed
- When a qcow2 source runs here on raw/qcow2 files, the blockdev format node keeps only options
  every format accepts.
- Job logs name the target node, and the home node when they differ.

### Tested
See the README's "Added in 1.1.0" table:
- console over HTTP and HTTPS
- qcow2 cold 257 MiB/s and live 148/123 ms
- return to another node: live (66 ms), cold with verify, and a container
- the storage pre-check

## 1.0.0 - 2026-11-08 - released as tag `v1.0.0`

The release is the initial commit (below) plus these changes from the same day:

### Added
- MIT license (`LICENSE`), linked from the README.
- README: "Use case: empty a node for maintenance or an upgrade". It covers live moving a guest out
  of a node, upgrading the node, then returning it live (older QEMU to newer, the direction
  rolling upgrades use) or cold. The section is marked **untested**, with any Proxmox version
  other than the tested one too.
- README: a note that a DHCP guest without a reservation can get a new address after a cold
  move or return (it is a reboot).

### Fixed
- Preflight counted a thin LVM volume as fully used (20 of 20 GiB), which made the free-space check
  too strict. It now reads `data_percent` from `lvs` (1.3 of 20 GiB in the test). Thick LVM still
  counts the full size, which is correct.

### Tested
The five setups 1.0.0 had only inherited from its predecessor were re-tested on a second portable
host, an AMD Ryzen 5 3500U laptop on Wi-Fi only:
- live VM move Intel -> AMD over Wi-Fi, HA-managed guest under constant writes: 274 ms downtime
- live return AMD -> Intel: 299 ms; HA re-added with its settings; all 400 in-flight writes intact
- cold move from LVM-thin with USB by vendor:product (attached on the host) and by port (dropped)
- cold return to LVM-thin, sha256 identical, USB config unchanged

## 1.0.0 initial commit - 2026-11-08

First public commit. proxmox-portable is a single-script rewrite of an earlier private,
multi-machine tool, which had a central web server driving agents on several laptops over SSH.
Now everything runs on the portable machine itself, and it manages only its own guests.

### Added
- `proxmox-portable.py`: one stdlib-only Python script that is the web console, the hypervisor
  side and the mover.
- Web console on port 8006 in Proxmox's layout:
  - Datacenter, nodes, guests, graphs, replication, backups, HA and tasks.
  - A branch for this host and its guests.
  - Light/dark theme.
- Login with a username and password from the config file:
  - plain or PBKDF2 hash (`hash-password`)
  - HTTPS with an auto-generated self-signed certificate
  - in-memory sessions
  - CSRF header check
  - login rate limit
  - optional `allowed_networks`
- One INI config file, `/etc/proxmox-portable/proxmox-portable.conf`, with an example containing
  placeholder values only.
- `setup`: installs the Debian packages, makes the directories and SSH key, adds the subuid lines,
  and writes and enables `proxmox-portable.service`.
- `fetch-qemu`: copies `pve-qemu-kvm`, OVMF and swtpm from a node and unpacks them beside Debian's
  packages.
- `check`: verifies the config, KVM, the API, root SSH to every node, the QEMU bundle, the tools and
  the LAN.
- The cluster can be read without an API token (`pvesh` over SSH) when none is configured.
- Its own NAT bridge (`ppnat0`, dnsmasq DHCP and DNS, nft masquerade), replacing libvirt's
  default network, so libvirt is no longer a dependency.
- A built-in sync scheduler in the service, replacing per-guest systemd timers.
- Several portable hosts can share a cluster: the tag is `<tag_prefix><host name>`, and each host
  manages only its own tag.

### Carried over (same logic as the predecessor)
- Live VM moves both ways:
  - dirty-bitmap pre-seed
  - incremental NBD mirror
  - auto-converge
  - stepped downtime limit
  - write-blocking fallback
- Cold VM moves with sha256-verified streams.
- Restart-mode container moves (rsync, two-pass frozen sync).
- Changed-block sync into the parked disk, under the `pvesr` lock and `lock: migrate`. EFI vars and
  TPM state are synced too.
- Live and cold returns. Tags, onboot and HA are restored.
- LAN mode keeps the guest's IP: routed /32, proxy ARP, static DHCP lease, bridge MAC rewrite,
  gratuitous ARP.
- Safety rails against split brain.

### Fixed (found while testing 1.0.0)
- LAN-mode DHCP failed after a cold move: dnsmasq drops to an unprivileged user and could not read
  its hosts file inside the 0700 config directory. The file now lives in `/run/proxmox-portable/`,
  and dnsmasq is restarted if it was started with another path.
- `setup` treated multi-arch packages as missing (`dpkg -s` calls them ambiguous) and re-ran apt
  every time. It now uses `dpkg-query`.

### Tested
See the README's "What has been tested" section: live, cold and container round trips, a UEFI+TPM
live round trip and a scheduled sync, on Proxmox VE 9.2 with a Debian 13 portable host.
