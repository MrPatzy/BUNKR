# Installing BUNKR on the Pi, the laptop and the hub

One script sets up every BUNKR machine: `deploy/install.sh`. You tell it what the machine is for
(its **role**) and it installs the right pieces.

| Role | Machine | What it gets |
| --- | --- | --- |
| `pi` | Raspberry Pi 5 field node | BUNKR, offline libraries (Kiwix), offline maps, radio and mesh programs, its own Wi-Fi hotspot "BUNKR", the name `bunkr.local` |
| `laptop` | Old laptop as a storage node | Same as the Pi, minus the hotspot. Only reachable from the laptop itself until you allow your home network |
| `hub` | The home hub (Proxmox container) | BUNKR only. Keeps the hub's existing firewall file and allowed host names |

Every run is safe to repeat. Re-running keeps the hotspot password, the pairing token and
downloads that are already there. It only changes what is different. To check first, add `--dry-run`.
The script then prints every step and changes nothing.

Supported systems: Raspberry Pi OS Bookworm (64-bit), Debian 12 or 13, Ubuntu 24.04, Linux Mint 22.

---

## 1. Raspberry Pi 5 field node (NVMe drive, SD card for recovery)

### What you need
- Raspberry Pi 5, official 27 W USB-C power supply, M.2 HAT+ (or another NVMe base board) and an NVMe SSD.
- The 512 GB SD card, kept as a recovery system.
- A computer with [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
- An Ethernet cable for the first setup. The hotspot takes over the Pi's Wi-Fi, so a Wi-Fi SSH session would drop.

### Steps
1. **Make the recovery SD card.** In Raspberry Pi Imager choose *Raspberry Pi 5*, then *Raspberry Pi OS (64-bit)*
   (or *Lite* for no desktop), then the SD card. Under *Edit settings* set a user name and password, turn on SSH,
   and set the Wi-Fi country to US. Write it.
2. **Fit the NVMe drive** on the HAT+ and boot the Pi from the SD card.
3. **Copy the system onto the NVMe drive.** On the Pi, open *Accessories → SD Card Copier*, copy from the SD card
   to the NVMe drive, and tick *New Partition UUIDs*. You can also flash the NVMe directly with Imager and a USB
   NVMe adapter.
4. **Boot from NVMe first and fall back to the SD card:**
   ```
   sudo rpi-eeprom-config --edit
   ```
   Set `BOOT_ORDER=0xf416`, save and reboot. The Pi tries NVMe first, then the SD card, then USB.
   If the NVMe drive ever fails, the SD card boots instead. If you use a non-HAT+ board, also add `PCIE_PROBE=1`.
5. **Check you booted from NVMe:** `findmnt /` should show `/dev/nvme0n1p2`.
6. **Get the BUNKR files onto the Pi**, either with `git clone` or by copying the folder from a USB stick, for example into `~/bunkr`.
7. **Run the installer** (plug in Ethernet first):
   ```
   cd ~/bunkr
   sudo ./deploy/install.sh --role pi --hub-url http://HUB-ADDRESS:8765
   ```
   Leave out `--hub-url` if you have no hub yet. You can re-run later with it.
   At the end the installer shows the **hotspot password once**. It is also saved in `/etc/bunkr/hotspot.txt`
   (read it with `sudo cat /etc/bunkr/hotspot.txt`). Write it on the case.
8. **Create your login:**
   ```
   cd /opt/bunkr && sudo -u bunkr python3 -m bunkr --db /var/lib/bunkr/bunkr.db user add YOURNAME --admin
   ```
9. **Pair with the hub** (at home, on Ethernet), then switch on the hourly sync:
   ```
   cd /opt/bunkr && sudo python3 -m bunkr pair --hub http://HUB-ADDRESS:8765 --token-file /etc/bunkr/node.token
   sudo chown bunkr:bunkr /etc/bunkr/node.token && sudo chmod 600 /etc/bunkr/node.token
   sudo systemctl enable --now bunkr-sync.timer
   ```
10. **Reboot.** The hotspot comes up on its own. If wlan0 was connected to your home Wi-Fi during the install,
    the hotspot was not switched on yet so it would not cut you off. It starts at this boot.

The hotspot is preferred over home Wi-Fi at boot, so the Pi always offers its own network in the field.
At home it reaches the hub over Ethernet.

### Joining from an iPhone (or any phone)
1. Open **Settings → Wi-Fi** and tap **BUNKR**. Enter the hotspot password.
2. The phone will say **"No Internet Connection"** under the network name. That is expected. Stay connected.
   If the phone keeps switching back to mobile data, turn off **Settings → Mobile Data** for a moment. You can
   also turn off **Wi-Fi Assist** at the bottom of the Mobile Data page.
3. Open **Safari** and type the address in full, including `http://`:
   - **http://bunkr.lan:8765** (or http://10.42.0.1:8765) opens the field kit page: family plan, maps, library,
     inventory, radio cards and app installers. No login on the hotspot: the Wi-Fi password is the gate.
     Making changes still needs your login (link at the bottom of the page).
   - **http://10.42.0.1:8080** opens the offline libraries (Wikipedia, medicine, repair guides).
   - **http://10.42.0.1:8765/store/web/maps/** opens the offline maps.
   - `http://bunkr.local:8765` also works on iPhones and Macs.
4. To make it one tap: in Safari tap **Share → Add to Home Screen**.

Anyone with the hotspot password can open the libraries. BUNKR itself still asks for a login.

---

## 2. Old laptop as a storage node (Debian or Linux Mint, XFCE)

The lost Windows password doesn't matter: installing Linux erases the whole disk.
**Copy off anything you want from the laptop first.** You can pull the disk, or boot the Linux USB stick in "try" mode and copy files to another drive.

1. **Make an installer USB stick** (8 GB or more) on another computer with Raspberry Pi Imager
   (*Use custom* image) or balenaEtcher:
   - Linux Mint XFCE: <https://linuxmint.com/download.php> (choose the *Xfce Edition*), or
   - Debian 13 with XFCE: <https://www.debian.org/distrib/> (the *netinst* image; pick *Xfce* during setup).
2. **Boot the laptop from the stick.** Switch it on and tap the boot-menu key. It's usually F12, F9, F11 or Esc;
   the start-up screen shows which. If the stick doesn't show up, turn off *Secure Boot* in the firmware settings
   (usually F2 or Del). Mint and Debian normally boot with Secure Boot on.
3. **Install:** choose *Erase disk and install*. Set a user name and password. On Debian, tick
   *Xfce* and *SSH server* in the software list.
4. **Keep it awake with the lid shut** (it is a storage node): edit `/etc/systemd/logind.conf`, set
   `HandleLidSwitch=ignore`, then `sudo systemctl restart systemd-logind`. Under *Settings → Power Manager*,
   set *Never* for sleep on mains power.
5. **Copy the BUNKR folder onto it** (for example into `~/bunkr`) and run:
   ```
   cd ~/bunkr
   sudo ./deploy/install.sh --role laptop --hub-url http://HUB-ADDRESS:8765 --allow-subnet 192.168.1.0/24
   ```
   `--allow-subnet` lets the rest of your home network reach it. Without it, only the laptop itself can open BUNKR.
   Use your own network's range (the installer prints the laptop's address).
6. **Create your login and pair**, the same as Pi steps 8 and 9.
7. **Give the laptop a fixed address** in your router (a DHCP reservation). BUNKR only answers to the addresses
   it knew at install time. If the address changes, re-run the installer.

Open it from another device at `http://LAPTOP-ADDRESS:8765`, with libraries on `:8080`.

---

## 3. The hub

The hub already runs BUNKR. When the app supports the new login and role options, update it with:
```
cd /opt/bunkr && sudo ./deploy/install.sh --role hub --dry-run   # look first
cd /opt/bunkr && sudo ./deploy/install.sh --role hub
```
This keeps `/etc/bunkr/firewall.nft` exactly as it is, and keeps the `--allow-host` names already in the service
(for example `bunkr.example.org`). Take a Proxmox snapshot of the container first.

---

## 4. Install with the .deb (Debian or Ubuntu, BUNKR only)

Each release also comes as a package, `bunkr_X.Y.Z_all.deb`, with `SHA256SUMS` and `SHA256SUMS.sig`. The package
installs BUNKR itself and nothing else: no hotspot, offline libraries, maps or radio programs (use the installer
above for a Pi or laptop node). A machine installed from the .deb can be upgraded from the Household tab
("Upgrade now").

1. Download the three files from the release page into one folder and check them:
   ```
   sha256sum -c SHA256SUMS
   ```
2. Install (this also installs Python, `python3-cryptography`, 7-Zip and `rsync` if they're missing):
   ```
   sudo apt install ./bunkr_X.Y.Z_all.deb
   ```
3. Set the options in `/etc/default/bunkr`. Out of the box BUNKR only answers on the machine itself
   (`--bind 127.0.0.1`). To reach it from your network, set `--bind 0.0.0.0`, add `--allow-host` for each name or
   address people type, and firewall port 8765 (`deploy/firewall.nft` is an example). Then
   `sudo systemctl restart bunkr`.
4. Create your login:
   ```
   sudo -u bunkr bunkr --db /var/lib/bunkr/bunkr.db user add YOURNAME --admin
   ```
5. Keep the .deb you installed from in `/var/lib/bunkr-upgrade/debs/` (`sudo install -m 600 bunkr_X.Y.Z_all.deb
   /var/lib/bunkr-upgrade/debs/`). The first upgrade from the Household tab uses it to go back if something goes
   wrong; without it, it downloads the same version again from the release page.

The app goes in `/usr/lib/bunkr`, its data in `/var/lib/bunkr`. `sudo apt remove bunkr` keeps the data;
`sudo apt purge bunkr` deletes `/var/lib/bunkr` too (database, store and backups).

The package won't install over a BUNKR that is a git checkout in `/opt/bunkr` or was set up by `deploy/install.sh`;
those keep upgrading the way they were installed.

### Moving to the .deb

A hub set up with `deploy/install.sh` can move to the package and keep its data:
1. Back up (snapshot, or copy `/var/lib/bunkr`).
2. Note the options after `--db ...` on the `ExecStart` line of `/etc/systemd/system/bunkr.service`.
3. `sudo systemctl disable --now bunkr` and `sudo mv /etc/systemd/system/bunkr.service ~/bunkr.service.old`.
4. Install the .deb as above, put the options you noted into `BUNKR_ARGS` in `/etc/default/bunkr`, and
   `sudo systemctl restart bunkr`. Your logins and data are where they were. Remove `/opt/bunkr` when it works.

---

## Options

| Option | Meaning |
| --- | --- |
| `--role pi\|laptop\|hub` | What this machine is for (required) |
| `--hub-url URL` | The hub, for example `http://192.168.1.40:8765`; sets up the hourly sync |
| `--hotspot-ssid NAME` | Wi-Fi network name for the Pi (default `BUNKR`) |
| `--no-hotspot` | Skip the Pi hotspot |
| `--no-radio` | Skip radio and mesh programs |
| `--allow-subnet CIDR` | Let this network reach ports 8765 and 8080 (repeatable) |
| `--allow-host NAME` | Another name or address BUNKR should answer to (repeatable) |
| `--wifi-country CC` | Wi-Fi country (default `US`) |
| `--hostname NAME` | Pi host name, reachable as `NAME.local` (default `bunkr`) |
| `--apt-cache` | Hub only: run an OS package cache (apt-cacher-ng, port 3142) so nodes update from home |
| `--apt-proxy URL` | Pi or laptop: use the hub's package cache, e.g. `http://192.168.1.40:3142`, whenever it answers; direct downloads otherwise |
| `--lock` | Pi or laptop: node lock (see below). Keeps the family plan, inventory and check-ins encrypted |
| `--chirp-file PATH` | A CHIRP file you downloaded (see below) |
| `--dry-run` | Show what would happen; change nothing |

## Node lock (a stolen Pi or laptop gives away nothing)

1. On the hub, open **Nodes** and set the **node-lock passphrase** (a few words the adults will remember; keep it
   with the document passphrase).
2. Install (or re-run the installer on) the Pi or laptop with `--lock`, and pair it as usual.
3. Within a minute of BUNKR starting at home, the node asks the hub for the key, moves its database into
   `/var/lib/bunkr/vault/bunkr.db.sealed`, and deletes the plain copy (and old plain backups in `db-backups`).
   The Nodes tab shows "locked data" on it.

After that the node unlocks by itself whenever it starts at home. Away from home (or with the hub down) its page
says **Locked**: maps, the library, radio files and apps still work, and anyone who knows the passphrase can unlock
it there. "Lock now" on the node's Home screen locks it by hand (admins). If a node is lost or stolen, press
**Lost or stolen** on it in the Nodes tab: it can no longer fetch the key, so only the passphrase protects what it
holds. On SD cards and SSDs, the old plain database may linger in unused blocks until they're reused; for a node
that already held data, the safest path is a fresh install with `--lock`.

## Optional AI on the hub

AI is off until an admin turns it on in the "AI (optional)" card on the Household tab. To use a free local model,
pick Ollama there and give its address. To use a command-line AI tool on the hub instead, set its command on the hub
itself (the web page only shows it, so a web login can never choose a program the hub runs). Either:

- add a line to `/var/lib/bunkr/ai.env` (mode 600, owned by `bunkr`; any other `NAME=value` lines there, such as the
  tool's login token, are passed to the tool):
  ```
  BUNKR_AI_CLI=llm -m mistral
  ```
- or start BUNKR with `--ai-cli "llm -m mistral"` (this wins over `ai.env`).

The command must read the prompt on standard input and print the reply; it is run directly, never through a shell.
Switch off the tool's own tools or plugins if it has any. BUNKR reads `ai.env` each time it uses AI, so no restart
is needed. To let an AI app use the AI connector (MCP), list its sign-in callback addresses (and
its web addresses, and optionally its server networks) on the same card.

## What goes where

| Path | What |
| --- | --- |
| `/opt/bunkr` | The app (copied from the folder you ran the installer from). From the .deb: `/usr/lib/bunkr`, options in `/etc/default/bunkr` |
| `/var/lib/bunkr/upgrade`, `/var/lib/bunkr-upgrade` | .deb installs: upgrade requests from the app; the upgrader's status, log and the last few `.deb`s (root only) |
| `/var/lib/bunkr/store/bunkr-releases` | Signed releases for upgrading with no internet |
| `/var/lib/bunkr/bunkr.db` | BUNKR's database |
| `/var/lib/bunkr/store/library` | Offline libraries (`*.zim`). Kiwix picks up new files by itself |
| `/var/lib/bunkr/store/maps` | Map files (`*.pmtiles`) |
| `/var/lib/bunkr/store/{tools,firmware,radio,web}` | Other synced downloads; `web/maps` holds the map viewer |
| `/etc/bunkr/hotspot.txt` | Hotspot name and password (root only) |
| `/etc/bunkr/node.token` | Pairing token from the hub |
| `/etc/bunkr/firewall.nft` | Firewall rules (rewritten by each run on the Pi and laptop) |
| `/var/cache/bunkr/downloads` | Verified downloads, reused on the next run |
| `/var/lib/bunkr/db-backups` | Database copies: nightly at 2 am (`bunkr-nightly-*`: the newest 14 plus the first of each month for 12 months), before each deploy (`bunkr-before-deploy-*`, newest 10) and by hand (never pruned). Change the numbers on Downloads > Space used. A restore test of the newest runs monthly; the result is on Home for admins. These stay on the same machine, so keep Proxmox backups of the container too |
| `/var/lib/bunkr/vapid.pem`, `ai.env` (optional AI), `firms.key`, `cloudflare-radar.token`, `mfa.key`, `vault.key` (hub) | Secrets in files (mode 600), never in the database, its backups or sync: the phone-notification key, the optional AI tool's login, the optional Situation source keys (entered on the Situation tab), the hub's node-lock key, and the key that seals authenticator-app secrets (back it up with the container: without it, codes stop working and people log in with a passkey or a recovery code, or an admin turns two-step off for them) |

Services: `bunkr` (port 8765), `bunkr-kiwix` (port 8080; Pi and laptop. On the hub BUNKR runs Kiwix itself on
8080, at `/kiwix/`), `bunkr-kiwix.path` (watches the library folder),
`bunkr-sync.timer` (hourly, after pairing), `bunkr-firewall`. Check any of them with
`systemctl status NAME` and `journalctl -u NAME -b`.

**Firewall:** the installer only adds its own table (`inet bunkr`) and only filters ports 8765 and 8080.
SSH and everything else are untouched. On the Pi, the hotspot network (10.42.0.0/24) and the Pi itself may
connect. On the laptop, only the laptop itself, plus any `--allow-subnet` networks.

## Offline maps

The viewer lists every `.pmtiles` file in `/var/lib/bunkr/store/maps`. It works with vector maps (for example a
Protomaps extract) and picture-tile maps. To make a Colorado map on any computer with internet, use the
[pmtiles tool](https://github.com/protomaps/go-pmtiles) to cut it from the current
[Protomaps daily build](https://maps.protomaps.com/builds/):
```
pmtiles extract https://build.protomaps.com/YYYYMMDD.pmtiles colorado.pmtiles --bbox=-109.1,36.9,-102.0,41.1
```
**No text labels, by design.** Map text needs font files (glyphs). Rather than depend on the internet or bundle a
font set, the viewer draws roads, water, parks, buildings and boundaries without words. Tap any place to see its name.

## Radio and mesh programs

Installed from the package archive when the system has them: **Dire Wolf** (packet/APRS), **fldigi** and **JS8Call**.
Also installed from their official releases with a checked SHA-256: **Pat** (Winlink) and **Mesh-client**
(Meshtastic, MeshCore, Reticulum). If one of them fails, the rest of the install carries on and the summary says what was skipped.

- **Radio cables and USB mesh boards:** run the installer with `sudo` from your own account. It then adds you to
  the `dialout`, `audio` and `plugdev` groups. Log out and back in afterwards.
- **Pat:** Pat's web page normally uses port 8080, which Kiwix uses here. Run `pat configure`, set your call sign
  and change `"http_addr"` to `"localhost:5000"`, then run `pat http` and open http://localhost:5000.
- **CHIRP** has to be added by hand. Its download site blocks scripted downloads, and the `chirp` package on PyPI
  is an unrelated program, so never `pip install chirp`.
  1. On any computer, open <https://archive.chirpmyradio.com/chirp_next/> and open the newest folder.
  2. Download, for the laptop (x86_64): `Chirp-next-DATE-x86_64.AppImage`.
     For the Pi (arm64): `chirp-DATE-py3-none-any.whl`.
  3. Put the file in `/var/lib/bunkr/store/radio/chirp/` (or pass `--chirp-file PATH`) and re-run the installer.
     On the Pi it installs wxPython from the package archive and CHIRP with pipx. That step needs internet once for
     CHIRP's small Python dependencies.

## Troubleshooting

- **The page says "bad host".** BUNKR only answers to names it was told about. Re-run the installer, adding
  `--allow-host THE-NAME-OR-ADDRESS`.
- **No "BUNKR" network appears.** Check `nmcli device` and `sudo nmcli connection up bunkr-hotspot`. The
  Wi-Fi country must be set; it is, unless you passed another `--wifi-country`.
- **An iPhone can't join.** Forget the network on the phone and re-enter the password. The hotspot uses WPA2
  with "protected management frames" turned off, which suits the Pi's Wi-Fi chip.
- **Libraries page is blank or won't load.** It only runs once a `.zim` file is in the library folder:
  `journalctl -u bunkr-kiwix -b`.
- **Sync never runs.** It waits for `/etc/bunkr/node.token`; see Pi step 9. Run one now with
  `sudo systemctl start bunkr-sync.service`.

## Upgrading

BUNKR never updates on its own. Once a day the hub asks the project's GitHub page for its latest release and, when
there's a newer one, admins get a note (the bell, and a push or email by their settings). The Household tab shows
what's new. You choose when, or whether, to upgrade; "Skip this version" quiets that one, and the check can be
turned off there.

### Installed from the .deb: "Upgrade now"

On a machine installed from the .deb, admins see **Upgrade now** on the Household tab when a newer signed release
is out. Pressing it:
1. BUNKR (which runs as the unprivileged `bunkr` user) leaves a request in `/var/lib/bunkr/upgrade/`. It never
   gets root itself.
2. `bunkr-upgrade.path` notices and starts `bunkr-upgrade.service` (root). It checks every field of the request,
   downloads that release's `.deb`, `SHA256SUMS` and `SHA256SUMS.sig`, and checks the signature with the release
   key built into the BUNKR you have, and the `.deb` against the signed checksum. Anything unsigned, wrongly
   signed, older, or from another repository is refused and nothing changes.
3. It backs up the database to `/var/lib/bunkr/db-backups/bunkr-before-upgrade-*.db` (newest 10 kept), installs
   the new `.deb`, restarts BUNKR and checks it answers.
4. If BUNKR doesn't come back, it reinstalls the previous `.deb` and puts the database backup back.

The card shows "Upgrading…", then "Upgraded to X.Y.Z" or "Rolled back: why". A new MAJOR version asks you to
confirm twice. Details are in `/var/lib/bunkr-upgrade/upgrade.log`; `sudo /usr/lib/bunkr/bunkr-upgrade --status`
prints the last result. To take releases from your own fork instead, set `BUNKR_UPGRADE_REPO=owner/name` in
`/etc/default/bunkr` (its releases must still be signed with the key in your BUNKR).

**With no internet** (a field node): copy a release's three files into `/var/lib/bunkr/store/bunkr-releases/`
(from a USB stick, for example). When they're signed and newer, the card offers "Upgrade now" from that copy.

By hand, the same thing: `sudo apt install ./bunkr_X.Y.Z_all.deb` after `sha256sum -c SHA256SUMS`.

### Installed with deploy/install.sh

To upgrade a machine:
1. Back up: snapshot the machine or container if you can, or at least copy `/var/lib/bunkr`. BUNKR also keeps
   its own database copies in `/var/lib/bunkr/db-backups`.
2. Update the folder you installed from: `git pull` in it, or copy in the new release's files.
3. From that folder: `sudo ./deploy/install.sh --role hub --dry-run` to look, then without `--dry-run`. Use the
   same role and options you installed with (`--role pi`, `--role laptop`, `--hub-url ...`).
4. BUNKR restarts; the database is updated on the first start. Upgrade the hub first, then the nodes.

A hub that runs from a git checkout with auto-deploy (below) updates from git, so its Household card says
"This hub updates from git" instead of offering the button.

Versions are numbered MAJOR.MINOR.PATCH. A new MAJOR may need a step by hand; its release notes say so.

## Auto-deploy on the hub

Merged pull requests go live by themselves. `bunkr-deploy.timer` runs `/usr/local/sbin/bunkr-autodeploy` (a root-owned
copy of `deploy/autodeploy.sh`) every 2 minutes. When `main` moves it tests the new commit as the `bunkr` user, backs up
the database to `/var/lib/bunkr/db-backups/bunkr-before-deploy-*.db` (newest 10 kept, or the number set on Downloads > Space used), fast-forwards `/opt/bunkr`,
restarts BUNKR and checks it answers. If tests fail nothing changes; if BUNKR doesn't come back, code and database
are rolled back. Either way it comments on the merged pull request. Log: `/var/lib/bunkr-deploy/deploy.log`.
It skips while `/opt/bunkr` has uncommitted or unpushed changes. To update the script itself after a change, run
`install -m 755 /opt/bunkr/deploy/autodeploy.sh /usr/local/sbin/bunkr-autodeploy` deliberately.
