# Changelog

All notable changes to proxmox-portable. Newest first.

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
