#!/usr/bin/env bash
# Project BUNKR installer for the hub, the Pi field node and the spare laptop.
#
#   sudo ./deploy/install.sh --role pi|laptop|hub [options]
#
# Safe to run again: it only changes what differs, keeps the hotspot password and paired token,
# and reuses downloads that are already present and verified. See deploy/INSTALL.md.
set -euo pipefail

# ---------------------------------------------------------------------------------------------
# Pinned downloads. Each SHA-256 was checked against the publisher before being written here.
# ---------------------------------------------------------------------------------------------
# Pat (Winlink client), https://github.com/la5nta/pat/releases/tag/v1.0.1
# Digests: GitHub release asset digests (the release publishes no separate checksum file).
PAT_VERSION="1.0.1"
declare -A PAT_SHA256=(
	[amd64]="21b5c7cbf96375d10da977a014c83d5bd11b2119d72b303db75b4ff168e163c0"
	[arm64]="4c82ca496d440ee2a6a32dc50f7304a40c899cbbfbebaf300ec260820310b82f"
	[armhf]="df10de04905ee3f39896204f73236a95d276400de85655696bc9917268c3f4d3"
)
# Mesh-client (Meshtastic, MeshCore, Reticulum), https://github.com/Colorado-Mesh/mesh-client/releases/tag/v6.8.0
MESH_VERSION="6.8.0"
declare -A MESH_SHA256=(
	[amd64]="778e6ef98eea8f0fcdbb1384302bee3ff3a6f7717056d5ec430c4bb7d5e4d299"
	[arm64]="66a8c4b243ba08bc52c4f76e0f0e9abf41a2851f216a2c8cfc40a1a9a4fc2729"
)
# Offline map viewer libraries (same bytes on jsDelivr, unpkg and cdnjs; pmtiles also matches the
# npm registry tarball's integrity hash).
MAPLIBRE_VERSION="5.24.0"
MAPLIBRE_JS_SHA256="45a9b07a9189ce56054c620a947ccf41e291e58c95e9b61533b740aaa65ee5cb"
MAPLIBRE_CSS_SHA256="ab1e70d59ec40465bae7e7030da2f3ccf28133fd502e62bd598eefbadfd7a732"
PMTILES_VERSION="4.5.0"
PMTILES_JS_SHA256="caf981bc46f6327ee7e65d5dc964d89d38a69f60edca2bd4c5c890c21b554c6c"
# Label fonts for the map viewer (Noto Sans glyph ranges; jsDelivr and GitHub copies checked identical).
FONTS_COMMIT="028c18f713baecad011301ff7a69acc39bcc2ae7"
FONT_FILES=(
	"Noto Sans Regular/0-255.pbf 62c6d49b15fa836eb6aa45e259c7ca6762f44b011b09e47776efbe4a6db1b397"
	"Noto Sans Regular/256-511.pbf 2eca7561f9f566bcacfda5dd04fb5880baec1328ec0f5484678289a13994de8a"
	"Noto Sans Regular/8192-8447.pbf 8ea977a587352fe31b4159ffdbc9a40be79056f2472017c742ea1e4a931864b9"
	"Noto Sans Medium/0-255.pbf ba2f0118dd024e3041b158e5f9eb49bc0a658019f53f458e9f5c0b8efcd79b91"
	"Noto Sans Medium/256-511.pbf d5e801a1a5b1d409d3298c3a1e1ca76328e2314a751078833a618620e8e66e4d"
	"Noto Sans Medium/8192-8447.pbf cc38e4956207f0edba1aaf749b61e8f6f1678ef1ad443d32ee285b21d0eb67aa"
	"OFL.txt 7713cfc8e3c36d5ec4aa3d6cffe7500a1b3310f8a86d914b8ea09c2a9dee7c2d"
)

# ---------------------------------------------------------------------------------------------
# Fixed locations
# ---------------------------------------------------------------------------------------------
APP_DIR="/opt/bunkr"
DATA_DIR="/var/lib/bunkr"
STORE="$DATA_DIR/store"
ETC_DIR="/etc/bunkr"
UNIT_DIR="/etc/systemd/system"
CACHE_DIR="/var/cache/bunkr/downloads"
TOKEN_FILE="$ETC_DIR/node.token"
HOTSPOT_FILE="$ETC_DIR/hotspot.txt"
NM_KEYFILE="/etc/NetworkManager/system-connections/bunkr-hotspot.nmconnection"
HOTSPOT_ADDR="10.42.0.1"
HOTSPOT_NET="10.42.0.0/24"
BUNKR_PORT=8765
KIWIX_PORT=8080
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------------------------
ROLE=""
HUB_URL=""
HOTSPOT_SSID="BUNKR"
SSID_GIVEN=0
HOTSPOT=1
NODE_LOCK=0
RADIO=1
DRY=0
WIFI_COUNTRY="US"
NEW_HOSTNAME="bunkr"
CHIRP_FILE=""
APT_CACHE=0
APT_PROXY=""
ALLOW_HOSTS=()
ALLOW_NETS=()

usage() {
	cat <<'EOF'
Usage: sudo ./deploy/install.sh --role pi|laptop|hub [options]

  --role ROLE           pi (field node with Wi-Fi hotspot), laptop (storage node) or hub
  --hub-url URL         the home hub, for example http://192.168.1.40:8765 (pi and laptop)
  --hotspot-ssid NAME   Wi-Fi network name for the Pi hotspot (default BUNKR)
  --no-hotspot          do not set up the Pi hotspot
  --no-radio            do not install radio and mesh programs
  --allow-subnet CIDR   also let this IPv4 network reach ports 8765 and 8080 (repeatable),
                        for example 192.168.1.0/24 to use the laptop from the home network
  --allow-host NAME     extra host name or address BUNKR should answer to (repeatable)
  --wifi-country CC     Wi-Fi country code for the Pi (default US)
  --hostname NAME       Pi host name, reachable as NAME.local (default bunkr)
  --chirp-file PATH     a CHIRP .whl or x86_64 .AppImage you downloaded (see INSTALL.md)
  --apt-cache           hub: run an OS package cache (apt-cacher-ng, port 3142) for the nodes
  --apt-proxy URL       pi/laptop: use the hub's package cache when reachable, for example
                        http://192.168.1.40:3142 (falls back to direct downloads when away)
  --lock                pi/laptop: node lock. The family plan, inventory and check-ins stay encrypted; the
                        node unlocks from the hub at home, or with the family passphrase (Nodes tab on the hub)
  --dry-run             only print what would be done; changes nothing
  -h, --help            show this help
EOF
}

need_value() {
	if [[ $# -lt 2 || -z $2 || $2 == --* ]]; then
		echo "Error: $1 needs a value." >&2
		exit 2
	fi
}

while (($#)); do
	case $1 in
	--role) need_value "$@"; ROLE=$2; shift 2 ;;
	--hub-url) need_value "$@"; HUB_URL=$2; shift 2 ;;
	--hotspot-ssid) need_value "$@"; HOTSPOT_SSID=$2; SSID_GIVEN=1; shift 2 ;;
	--no-hotspot) HOTSPOT=0; shift ;;
	--lock) NODE_LOCK=1; shift ;;
	--no-radio) RADIO=0; shift ;;
	--allow-subnet) need_value "$@"; ALLOW_NETS+=("$2"); shift 2 ;;
	--allow-host) need_value "$@"; ALLOW_HOSTS+=("$2"); shift 2 ;;
	--wifi-country) need_value "$@"; WIFI_COUNTRY=$2; shift 2 ;;
	--hostname) need_value "$@"; NEW_HOSTNAME=$2; shift 2 ;;
	--chirp-file) need_value "$@"; CHIRP_FILE=$2; shift 2 ;;
	--apt-cache) APT_CACHE=1; shift ;;
	--apt-proxy) need_value "$@"; APT_PROXY=$2; shift 2 ;;
	--dry-run) DRY=1; shift ;;
	-h | --help) usage; exit 0 ;;
	*) echo "Error: unknown option '$1'." >&2; usage >&2; exit 2 ;;
	esac
done

if [[ -n $APT_PROXY && ! $APT_PROXY =~ ^http://[A-Za-z0-9._-]+:[0-9]+/?$ ]]; then
	echo "Error: --apt-proxy must look like http://HUB:3142 (got '$APT_PROXY')." >&2
	exit 2
fi

case $ROLE in
pi | laptop | hub) ;;
"") echo "Error: --role is required." >&2; usage >&2; exit 2 ;;
*) echo "Error: --role must be pi, laptop or hub (got '$ROLE')." >&2; exit 2 ;;
esac
if [[ -n $HUB_URL ]]; then
	url_re='^https?://[A-Za-z0-9._-]+(:[0-9]+)?(/[^[:space:]]*)?$'
	if [[ ! $HUB_URL =~ $url_re ]]; then
		echo "Error: --hub-url must look like http://HUB:8765 (got '$HUB_URL')." >&2
		exit 2
	fi
	HUB_URL=${HUB_URL%/}
fi
if [[ ! $HOTSPOT_SSID =~ ^[A-Za-z0-9._\ -]{1,32}$ ]]; then
	echo "Error: --hotspot-ssid may use letters, digits, space, . _ - and be at most 32 characters." >&2
	exit 2
fi
if [[ ! $WIFI_COUNTRY =~ ^[A-Z]{2}$ ]]; then
	echo "Error: --wifi-country must be two capital letters, like US." >&2
	exit 2
fi
if [[ ! $NEW_HOSTNAME =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
	echo "Error: --hostname must be lower-case letters, digits and dashes." >&2
	exit 2
fi
for net in "${ALLOW_NETS[@]}"; do
	if [[ ! $net =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$ ]]; then
		echo "Error: --allow-subnet takes an IPv4 network like 192.168.1.0/24 (got '$net')." >&2
		exit 2
	fi
done
for h in "${ALLOW_HOSTS[@]}"; do
	if [[ ! $h =~ ^[A-Za-z0-9._:-]+$ ]]; then
		echo "Error: --allow-host '$h' is not a plain host name or address." >&2
		exit 2
	fi
done
IS_NODE=0
[[ $ROLE == pi || $ROLE == laptop ]] && IS_NODE=1

# ---------------------------------------------------------------------------------------------
# Output and bookkeeping helpers
# ---------------------------------------------------------------------------------------------
LOG_DIR="$(mktemp -d)"
trap 'rm -rf "$LOG_DIR"' EXIT
: >"$LOG_DIR/skipped"
: >"$LOG_DIR/warnings"
: >"$LOG_DIR/notes"
STEP=0

step() {
	STEP=$((STEP + 1))
	printf '\n== Step %d: %s\n' "$STEP" "$*"
}
info() { printf '   %s\n' "$*"; }
warn() {
	printf '   WARNING: %s\n' "$*" >&2
	printf '%s\n' "$*" >>"$LOG_DIR/warnings"
}
# skipped NAME REASON: recorded for the summary.
skipped() {
	info "Skipping $1: $2"
	printf '%s: %s\n' "$1" "$2" >>"$LOG_DIR/skipped"
}
note() { printf '%s\n' "$*" >>"$LOG_DIR/notes"; }

# run CMD...: run a command, or only show it in dry-run mode.
run() {
	if ((DRY)); then
		printf '   [dry-run] would run:'
		printf ' %q' "$@"
		printf '\n'
	else
		"$@"
	fi
}

# put_file PATH MODE OWNER:GROUP [brief] < content: write a file only if its content or mode changed.
# With "brief", a dry run names the file without printing its content.
put_file() {
	local path=$1 mode=$2 owner=$3 brief=${4:-} tmp
	tmp="$(mktemp)"
	cat >"$tmp"
	if ((DRY)); then
		if [[ -f $path ]] && cmp -s "$tmp" "$path"; then
			info "[dry-run] $path is already up to date"
		else
			if [[ -n $brief ]]; then
				info "[dry-run] would write $path (mode $mode, owner $owner, $(wc -l <"$tmp") lines)"
			else
				info "[dry-run] would write $path (mode $mode, owner $owner):"
				sed 's/^/        | /' "$tmp"
			fi
		fi
		rm -f "$tmp"
		return 0
	fi
	if [[ -f $path ]] && cmp -s "$tmp" "$path"; then
		rm -f "$tmp"
		chmod "$mode" "$path"
		chown "$owner" "$path"
		info "Up to date: $path"
		return 0
	fi
	install -D -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$tmp" "$path"
	rm -f "$tmp"
	info "Wrote $path"
}

# optional NAME FUNCTION: run a step whose failure must not stop the whole install.
optional() {
	local name=$1 rc
	shift
	set +e
	(
		set -e
		"$@"
	)
	rc=$?
	set -e
	if ((rc != 0)); then
		warn "$name did not finish (exit code $rc). The rest of the install continues; see the messages above."
		printf '%s: failed, see the messages above\n' "$name" >>"$LOG_DIR/skipped"
	fi
	return 0
}

pkg_available() {
	local cand
	cand="$(apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ {print $2; exit}')"
	[[ -n $cand && $cand != "(none)" ]]
}

pkg_installed() {
	[[ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null || true)" == "install ok installed" ]]
}

APT_UPDATED=0
apt_update_once() {
	if ((APT_UPDATED == 0)); then
		run apt-get update
		APT_UPDATED=1
	fi
}

apt_install() {
	run env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
}

sha256_of() { sha256sum "$1" | awk '{print $1}'; }

# fetch URL SHA256 NAME: download to the cache (or reuse a verified copy) and print the local path.
fetch() {
	local url=$1 sha=$2 name=$3 dest="$CACHE_DIR/$3" cand
	if ((DRY)); then
		info "[dry-run] would download $url" >&2
		info "[dry-run]   and check SHA-256 $sha" >&2
		printf '%s\n' "$dest"
		return 0
	fi
	for cand in "$dest" "$STORE/radio/$name" "$STORE/tools/$name"; do
		if [[ -f $cand ]] && [[ "$(sha256_of "$cand")" == "$sha" ]]; then
			info "Using verified copy $cand" >&2
			printf '%s\n' "$cand"
			return 0
		fi
	done
	install -d -m 0755 "$CACHE_DIR"
	info "Downloading $url" >&2
	curl -fL --retry 3 --connect-timeout 20 -A "bunkr-installer" -o "$dest.part" "$url" >&2
	if [[ "$(sha256_of "$dest.part")" != "$sha" ]]; then
		rm -f "$dest.part"
		echo "   Checksum mismatch for $name; the download was deleted." >&2
		return 1
	fi
	mv -f "$dest.part" "$dest"
	printf '%s\n' "$dest"
}

# install_deb FILE: install a downloaded .deb unless that exact version is already installed.
install_deb() {
	local deb=$1 pkg ver have
	if ((DRY)); then
		run apt-get install -y "$deb"
		return 0
	fi
	pkg="$(dpkg-deb -f "$deb" Package)"
	ver="$(dpkg-deb -f "$deb" Version)"
	have="$(dpkg-query -W -f='${Version}' "$pkg" 2>/dev/null || true)"
	if [[ $have == "$ver" ]] && pkg_installed "$pkg"; then
		info "$pkg $ver is already installed."
		return 0
	fi
	apt_install "$deb"
}

ipv4_addrs() {
	hostname -I 2>/dev/null | tr ' ' '\n' | awk '/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/'
}

# ---------------------------------------------------------------------------------------------
# Step 0: checks
# ---------------------------------------------------------------------------------------------
printf 'Project BUNKR installer: role %s%s\n' "$ROLE" "$( ((DRY)) && echo ' (dry run: nothing will be changed)')"

if ((!DRY)) && [[ $EUID -ne 0 ]]; then
	echo "Error: run this with sudo (or add --dry-run to just see what it would do)." >&2
	exit 1
fi
if [[ ! -f $SRC_DIR/bunkr/__main__.py ]]; then
	echo "Error: $SRC_DIR does not look like the BUNKR source folder." >&2
	exit 1
fi
if ! command -v apt-get >/dev/null 2>&1; then
	echo "Error: this installer needs a Debian-family system (Debian, Raspberry Pi OS, Ubuntu, Mint)." >&2
	exit 1
fi

OS_NAME="unknown"
if [[ -r /etc/os-release ]]; then
	# shellcheck disable=SC1091
	OS_NAME="$(. /etc/os-release && echo "${PRETTY_NAME:-$ID}")"
fi
ARCH="$(dpkg --print-architecture)"
PI_MODEL=""
if [[ -r /proc/device-tree/model ]]; then
	PI_MODEL="$(tr -d '\0' </proc/device-tree/model)"
fi
info "System: $OS_NAME, architecture $ARCH${PI_MODEL:+, $PI_MODEL}"
info "Installing from: $SRC_DIR"
if [[ $ROLE == pi && $PI_MODEL != Raspberry* ]]; then
	warn "Role is pi but this does not look like a Raspberry Pi. Continuing anyway."
fi
if ((IS_NODE)) && [[ -z $HUB_URL ]]; then
	info "No --hub-url given, so automatic sync from the hub will not be set up."
fi
if [[ $ROLE == hub && -n $HUB_URL ]]; then
	info "--hub-url is ignored for the hub role."
	HUB_URL=""
fi

# ---------------------------------------------------------------------------------------------
# Step 1: base packages
# ---------------------------------------------------------------------------------------------
step "Install the base programs"
apt_update_once
BASE_PKGS=(python3 python3-cryptography rsync nftables curl ca-certificates)
if pkg_available 7zip; then
	BASE_PKGS+=(7zip)
else
	info "Package 7zip is not offered here; using p7zip-full instead."
	BASE_PKGS+=(p7zip-full)
fi
if pkg_available kiwix-tools; then
	BASE_PKGS+=(kiwix-tools)
else
	warn "Package kiwix-tools is not available, so offline libraries cannot be served on port $KIWIX_PORT."
fi
if [[ $ROLE == pi ]]; then
	BASE_PKGS+=(avahi-daemon)
	if ((HOTSPOT)); then
		BASE_PKGS+=(network-manager dnsmasq-base)
	fi
fi
apt_install "${BASE_PKGS[@]}"

if command -v python3 >/dev/null 2>&1 && ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
	warn "BUNKR is developed on Python 3.12; this system has $(python3 -V 2>&1). It may work, but test it."
fi

# ---------------------------------------------------------------------------------------------
# Step 2: the BUNKR app, its user and folders
# ---------------------------------------------------------------------------------------------
step "Install the BUNKR app into $APP_DIR"
if [[ $SRC_DIR -ef $APP_DIR ]]; then
	info "Already running from $APP_DIR; nothing to copy."
else
	run install -d -m 0755 "$APP_DIR"
	run rsync -a --delete --no-owner --no-group --chmod=go-w \
		--exclude .git --exclude __pycache__ --exclude '*.pyc' \
		"$SRC_DIR/" "$APP_DIR/"
fi

if getent passwd bunkr >/dev/null; then
	info "System user bunkr already exists."
else
	run useradd --system --user-group --home-dir "$DATA_DIR" --shell /usr/sbin/nologin bunkr
fi
run install -d -o bunkr -g bunkr -m 0750 "$DATA_DIR"
run install -d -o bunkr -g bunkr -m 0755 "$STORE"
for sub in library tools maps firmware radio web web/maps; do
	run install -d -o bunkr -g bunkr -m 0755 "$STORE/$sub"
done
run install -d -o bunkr -g bunkr -m 0750 "$DATA_DIR/kiwix"
# The bunkr user must be able to read node.token; the hotspot password file stays root-only.
run install -d -o root -g bunkr -m 0750 "$ETC_DIR"

# Host names BUNKR answers to. The app refuses requests whose Host header is not on this list.
HOSTS=()
add_host() {
	local h
	for h in "$@"; do
		h=${h,,}
		[[ -z $h ]] && continue
		[[ " ${HOSTS[*]} " == *" $h "* ]] || HOSTS+=("$h")
	done
}
if [[ $ROLE == hub && -f $UNIT_DIR/bunkr.service ]]; then
	# Keep the names already configured on the hub (for example its tunnel host name).
	mapfile -t old_hosts < <(grep -oE -- '--allow-host [^ ]+' "$UNIT_DIR/bunkr.service" | awk '{print $2}' || true)
	if ((${#old_hosts[@]})); then
		info "Keeping existing allowed host names: ${old_hosts[*]}"
		add_host "${old_hosts[@]}"
	fi
fi
add_host "${ALLOW_HOSTS[@]}"
if ((IS_NODE)); then
	if [[ $ROLE == pi ]]; then
		add_host "$NEW_HOSTNAME" "$NEW_HOSTNAME.local" "$NEW_HOSTNAME.lan"
		((HOTSPOT)) && add_host "$HOTSPOT_ADDR"
	else
		add_host "$(hostname -s 2>/dev/null || true)" "$(hostname -s 2>/dev/null || true).local"
	fi
	mapfile -t addrs < <(ipv4_addrs)
	add_host "${addrs[@]}"
fi

EXEC=(/usr/bin/python3 -m bunkr --db "$DATA_DIR/bunkr.db" --bind 0.0.0.0 --port "$BUNKR_PORT" --no-browser
	--role "$ROLE" --require-login)
[[ -n $HUB_URL ]] && EXEC+=(--hub-url "$HUB_URL")
# On the field Wi-Fi the Wi-Fi password is the gate: hotspot clients get the read-only field page.
[[ $ROLE == pi ]] && ((HOTSPOT)) && EXEC+=(--open-net "$HOTSPOT_NET")
# Node lock: the database lives encrypted in $DATA_DIR/vault; BUNKR asks the hub for the key with the sync token.
((IS_NODE)) && ((NODE_LOCK)) && EXEC+=(--lock --token-file "$TOKEN_FILE")
for h in "${HOSTS[@]}"; do EXEC+=(--allow-host "$h"); done

step "Install the BUNKR service"
case $ROLE in
pi) DESC="Project BUNKR field node" ;;
laptop) DESC="Project BUNKR storage node" ;;
hub) DESC="Project BUNKR preparedness hub" ;;
esac
put_file "$UNIT_DIR/bunkr.service" 0644 root:root <<EOF
[Unit]
Description=$DESC
After=network-online.target bunkr-firewall.service
Requires=bunkr-firewall.service

[Service]
User=bunkr
Group=bunkr
WorkingDirectory=$APP_DIR
Environment=HOME=$DATA_DIR
ExecStart=${EXEC[*]}
Restart=on-failure
NoNewPrivileges=yes
ProtectSystem=full
PrivateTmp=yes
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF

put_file "$UNIT_DIR/bunkr-firewall.service" 0644 root:root <<EOF
[Unit]
Description=Firewall for the BUNKR web ports
Before=network-pre.target bunkr.service
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f $ETC_DIR/firewall.nft
ExecStop=/usr/sbin/nft delete table inet bunkr

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------------------------------------
# Step 3: hourly sync from the hub (nodes)
# ---------------------------------------------------------------------------------------------
setup_sync() {
	put_file "$UNIT_DIR/bunkr-sync.service" 0644 root:root <<EOF
[Unit]
Description=BUNKR: fetch updates from the hub
After=network-online.target
Wants=network-online.target
ConditionPathExists=$TOKEN_FILE

[Service]
Type=oneshot
User=bunkr
Group=bunkr
WorkingDirectory=$APP_DIR
Environment=HOME=$DATA_DIR
ExecStart=/usr/bin/python3 -m bunkr --db $DATA_DIR/bunkr.db sync --hub $HUB_URL --token-file $TOKEN_FILE --store $STORE
Nice=10
IOSchedulingClass=idle
NoNewPrivileges=yes
ProtectSystem=full
PrivateTmp=yes
ReadWritePaths=$DATA_DIR
EOF
	put_file "$UNIT_DIR/bunkr-sync.timer" 0644 root:root <<EOF
[Unit]
Description=BUNKR: fetch updates from the hub every hour

[Timer]
OnCalendar=hourly
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF
}
# OS package cache: the hub caches .deb downloads so a stored laptop or the Pi can update
# from the home network. Nodes use it only when it answers, so updates still work away from home.
if [[ $ROLE == hub ]] && ((APT_CACHE)); then
	step "Set up the OS package cache (apt-cacher-ng on port 3142)"
	if pkg_available apt-cacher-ng; then
		echo "apt-cacher-ng apt-cacher-ng/tunnelenable boolean false" | run debconf-set-selections
		apt_install apt-cacher-ng
		run systemctl enable --now apt-cacher-ng
		info "Nodes can use it with: --apt-proxy http://<this hub's address>:3142"
	else
		skipped "apt-cacher-ng is not available from this system's package sources"
	fi
fi
if ((IS_NODE)) && [[ -n $APT_PROXY ]]; then
	step "Use the hub's package cache when it is reachable ($APT_PROXY)"
	run install -d -m 0755 /usr/local/lib/bunkr
	put_file /usr/local/lib/bunkr/apt-proxy-detect 0755 root:root <<EOF
#!/bin/bash
# Print the hub's package cache if it answers within 2 seconds, otherwise DIRECT.
proxy="${APT_PROXY%/}"
hostport="\${proxy#http://}"
if timeout 2 bash -c "exec 3<>/dev/tcp/\${hostport%:*}/\${hostport##*:}" 2>/dev/null; then
	echo "\$proxy"
else
	echo DIRECT
fi
EOF
	put_file /etc/apt/apt.conf.d/30bunkr-proxy 0644 root:root <<EOF
// Written by BUNKR's install.sh: use the home hub's package cache when reachable.
Acquire::http::Proxy-Auto-Detect "/usr/local/lib/bunkr/apt-proxy-detect";
EOF
fi

if ((IS_NODE)) && [[ -n $HUB_URL ]]; then
	step "Set up hourly sync from the hub ($HUB_URL)"
	setup_sync
fi

# ---------------------------------------------------------------------------------------------
# Step 4: Kiwix offline libraries on port 8080 (nodes)
# ---------------------------------------------------------------------------------------------
setup_kiwix() {
	put_file "$UNIT_DIR/bunkr-kiwix.service" 0644 root:root <<EOF
[Unit]
Description=BUNKR offline libraries (Kiwix) on port $KIWIX_PORT
After=network-online.target bunkr-firewall.service
Requires=bunkr-firewall.service

[Service]
User=bunkr
Group=bunkr
Environment=BUNKR_LIBRARY_DIR=$STORE/library BUNKR_KIWIX_STATE=$DATA_DIR/kiwix BUNKR_KIWIX_PORT=$KIWIX_PORT
# Exits with status 0 when there are no ZIM files yet, so it is not restarted in a loop.
ExecStart=/bin/bash $APP_DIR/deploy/pi/kiwix-serve.sh
Restart=on-failure
RestartSec=15
NoNewPrivileges=yes
ProtectSystem=full
PrivateTmp=yes
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF
	put_file "$UNIT_DIR/bunkr-kiwix-refresh.service" 0644 root:root <<EOF
[Unit]
Description=BUNKR: restart Kiwix after the library folder changed

[Service]
Type=oneshot
# Wait for a burst of copies to settle before restarting.
ExecStartPre=/bin/sleep 20
ExecStart=/bin/systemctl restart bunkr-kiwix.service
EOF
	put_file "$UNIT_DIR/bunkr-kiwix.path" 0644 root:root <<EOF
[Unit]
Description=BUNKR: watch the offline library folder

[Path]
PathChanged=$STORE/library
Unit=bunkr-kiwix-refresh.service

[Install]
WantedBy=paths.target
EOF
}
KIWIX_OK=0
if ((IS_NODE)); then
	step "Set up offline libraries (Kiwix) on port $KIWIX_PORT"
	if ((DRY)) || command -v kiwix-serve >/dev/null 2>&1; then
		setup_kiwix
		KIWIX_OK=1
	else
		skipped "Kiwix" "kiwix-serve is not installed"
	fi
fi

# ---------------------------------------------------------------------------------------------
# Step 5: Pi hotspot and bunkr.local
# ---------------------------------------------------------------------------------------------
setup_hotspot() {
	local pass="" uuid="" ssid=$HOTSPOT_SSID wstate wconn new=0
	if ! command -v nmcli >/dev/null 2>&1 || ! systemctl is-active --quiet NetworkManager; then
		if ((DRY)); then
			info "[dry-run] NetworkManager is not running here; a real run would stop at this point."
		else
			skipped "Hotspot" "NetworkManager is not running"
			return 0
		fi
	elif ! nmcli -t -f DEVICE,TYPE device | grep -qx 'wlan0:wifi'; then
		skipped "Hotspot" "no Wi-Fi device named wlan0"
		return 0
	fi

	if [[ -f $HOTSPOT_FILE ]]; then
		pass="$(sed -n 's/^PASSWORD=//p' "$HOTSPOT_FILE" | head -n 1)"
		((SSID_GIVEN)) || ssid="$(sed -n 's/^SSID=//p' "$HOTSPOT_FILE" | head -n 1)"
		[[ -n $ssid ]] || ssid=$HOTSPOT_SSID
	fi
	if [[ -z $pass ]]; then
		if ((DRY)); then
			pass="(random password made on the real run)"
		else
			pass="$(python3 -c 'import secrets; a="abcdefghjkmnpqrstuvwxyzACDEFGHJKLMNPQRSTUVWXYZ23456789"; print("".join(secrets.choice(a) for _ in range(16)))')"
		fi
		new=1
	fi
	put_file "$HOTSPOT_FILE" 0600 root:root <<EOF
# BUNKR Wi-Fi hotspot. Join this network, then open http://$HOTSPOT_ADDR:$BUNKR_PORT
SSID=$ssid
PASSWORD=$pass
EOF
	if ((new)) && ((!DRY)); then
		printf 'SSID=%s\nPASSWORD=%s\n' "$ssid" "$pass" >"$LOG_DIR/hotspot_new"
	fi

	# Wi-Fi stays blocked on Raspberry Pi OS until a country is set.
	if command -v raspi-config >/dev/null 2>&1; then
		run raspi-config nonint do_wifi_country "$WIFI_COUNTRY"
	fi
	if command -v rfkill >/dev/null 2>&1; then
		run rfkill unblock wifi
	fi

	if [[ -f $NM_KEYFILE ]]; then
		uuid="$(sed -n 's/^uuid=//p' "$NM_KEYFILE" | head -n 1)"
	fi
	[[ -n $uuid ]] || uuid="$(cat /proc/sys/kernel/random/uuid)"
	# Written as a NetworkManager keyfile (root-only) so the password never appears on a command line.
	# pmf=1 turns off protected management frames, which the Pi's Wi-Fi chip handles badly in hotspot
	# mode and which stops some phones from joining.
	if ((DRY)); then
		info "[dry-run] would write $NM_KEYFILE (mode 0600): Wi-Fi hotspot '$ssid' on wlan0, WPA2,"
		info "[dry-run]   address $HOTSPOT_ADDR/24 shared, starts automatically at boot"
	else
		put_file "$NM_KEYFILE" 0600 root:root <<EOF
[connection]
id=bunkr-hotspot
uuid=$uuid
type=wifi
interface-name=wlan0
autoconnect=true
autoconnect-priority=50

[wifi]
mode=ap
ssid=$ssid
band=bg
channel=6

[wifi-security]
key-mgmt=wpa-psk
proto=rsn;
pairwise=ccmp;
group=ccmp;
pmf=1
psk=$pass

[ipv4]
method=shared
address1=$HOTSPOT_ADDR/24

[ipv6]
method=disabled
EOF
	fi
	# Friendly name for phones that use the hotspot's DNS: http://$NEW_HOSTNAME.lan:8765
	put_file /etc/NetworkManager/dnsmasq-shared.d/bunkr.conf 0644 root:root <<EOF
# Added by the BUNKR installer: answer $NEW_HOSTNAME.lan on the hotspot.
address=/$NEW_HOSTNAME.lan/$HOTSPOT_ADDR
EOF
	run nmcli connection load "$NM_KEYFILE"

	wstate="$(nmcli -t -f DEVICE,STATE,CONNECTION device 2>/dev/null | awk -F: '$1=="wlan0" {print $2; exit}' || true)"
	wconn="$(nmcli -t -f DEVICE,STATE,CONNECTION device 2>/dev/null | awk -F: '$1=="wlan0" {print $3; exit}' || true)"
	if [[ $wstate == connected && $wconn != bunkr-hotspot ]]; then
		# Switching now would drop this Pi off the Wi-Fi network it is using (and maybe your SSH session).
		info "wlan0 is connected to '$wconn', so the hotspot is not switched on now."
		note "The hotspot starts at the next boot, or now with: sudo nmcli connection up bunkr-hotspot (this ends the current Wi-Fi connection; use Ethernet for SSH)."
	elif ((DRY)); then
		run nmcli connection up bunkr-hotspot
	elif nmcli connection up bunkr-hotspot >/dev/null; then
		info "Hotspot '$ssid' is on."
	else
		warn "The hotspot could not be switched on now. Check: journalctl -u NetworkManager -b"
	fi
	note "Hotspot: network '$ssid', password stored in $HOTSPOT_FILE (sudo cat $HOTSPOT_FILE)."
}

setup_avahi() {
	local current
	current="$(hostname)"
	if [[ $current != "$NEW_HOSTNAME" ]]; then
		run hostnamectl set-hostname "$NEW_HOSTNAME"
		if grep -q '^127\.0\.1\.1' /etc/hosts; then
			run sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$NEW_HOSTNAME/" /etc/hosts
		else
			if ((DRY)); then
				info "[dry-run] would add '127.0.1.1 $NEW_HOSTNAME' to /etc/hosts"
			else
				printf '127.0.1.1\t%s\n' "$NEW_HOSTNAME" >>/etc/hosts
			fi
		fi
	else
		info "Host name is already $NEW_HOSTNAME."
	fi
	put_file /etc/avahi/services/bunkr.service 0644 root:root <<EOF
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">BUNKR on %h</name>
  <service>
    <type>_http._tcp</type>
    <port>$BUNKR_PORT</port>
  </service>
</service-group>
EOF
	run systemctl enable avahi-daemon.service
	run systemctl restart avahi-daemon.service
}

if [[ $ROLE == pi ]]; then
	step "Set up the Wi-Fi hotspot"
	if ((HOTSPOT)); then
		optional "Hotspot" setup_hotspot
	else
		skipped "Hotspot" "--no-hotspot was given"
	fi
	step "Make the Pi reachable as $NEW_HOSTNAME.local"
	optional "bunkr.local name" setup_avahi
fi

# ---------------------------------------------------------------------------------------------
# Step 6: firewall (own table "inet bunkr"; other tables are never touched)
# ---------------------------------------------------------------------------------------------
step "Set up the BUNKR firewall"
write_node_firewall() {
	local nets=() n elements="" tmp
	[[ $ROLE == pi ]] && ((HOTSPOT)) && nets+=("$HOTSPOT_NET")
	nets+=("${ALLOW_NETS[@]}")
	for n in "${nets[@]}"; do elements+="${elements:+, }$n"; done
	{
		echo "# BUNKR ports $BUNKR_PORT (app) and $KIWIX_PORT (libraries). Written by deploy/install.sh;"
		echo "# re-running it rewrites this file, so add networks with --allow-subnet instead of editing."
		echo "# Only these two ports are filtered: SSH and everything else are left alone."
		echo "table inet bunkr"
		echo "delete table inet bunkr"
		echo "table inet bunkr {"
		echo "	set allowed4 {"
		echo "		type ipv4_addr"
		echo "		flags interval"
		[[ -n $elements ]] && echo "		elements = { $elements }"
		echo "	}"
		echo ""
		echo "	chain input {"
		echo "		type filter hook input priority -10; policy accept;"
		echo "		tcp dport { $BUNKR_PORT, $KIWIX_PORT } iif \"lo\" accept"
		echo "		tcp dport { $BUNKR_PORT, $KIWIX_PORT } ip saddr @allowed4 counter accept"
		echo "		tcp dport { $BUNKR_PORT, $KIWIX_PORT } counter drop"
		echo "	}"
		echo "}"
	} >"$LOG_DIR/firewall.nft"
	if ((!DRY)); then
		tmp="$LOG_DIR/firewall.nft"
		if ! nft -c -f "$tmp"; then
			echo "   The generated firewall rules did not pass nft's check; nothing was loaded." >&2
			return 1
		fi
	fi
	put_file "$ETC_DIR/firewall.nft" 0644 root:root <"$LOG_DIR/firewall.nft"
	if [[ -n $elements ]]; then
		info "Ports $BUNKR_PORT and $KIWIX_PORT: allowed from this machine and $elements; blocked for everyone else."
	else
		info "Ports $BUNKR_PORT and $KIWIX_PORT: allowed from this machine only (add networks with --allow-subnet)."
	fi
}
if ((IS_NODE)); then
	write_node_firewall
elif [[ -f $ETC_DIR/firewall.nft ]]; then
	info "Keeping the hub's existing $ETC_DIR/firewall.nft (edit it by hand; this installer leaves it alone)."
else
	put_file "$ETC_DIR/firewall.nft" 0644 root:root brief <"$SRC_DIR/deploy/firewall.nft"
	warn "Installed the sample hub firewall from deploy/firewall.nft. Check the allowed addresses in $ETC_DIR/firewall.nft."
fi

# ---------------------------------------------------------------------------------------------
# Start services
# ---------------------------------------------------------------------------------------------
step "Start the services"
run systemctl daemon-reload
run systemctl enable bunkr-firewall.service bunkr.service
run systemctl restart bunkr-firewall.service
run systemctl restart bunkr.service
if ((KIWIX_OK)); then
	run systemctl enable bunkr-kiwix.service bunkr-kiwix.path
	run systemctl restart bunkr-kiwix.path
	run systemctl restart bunkr-kiwix.service
fi
PAIRED=0
if ((IS_NODE)) && [[ -n $HUB_URL ]]; then
	if [[ -f $TOKEN_FILE ]]; then
		PAIRED=1
		run systemctl enable --now bunkr-sync.timer
	else
		info "Not paired with the hub yet, so the hourly sync is installed but not switched on."
	fi
fi

# ---------------------------------------------------------------------------------------------
# Step 7: radio and mesh programs (nodes)
# ---------------------------------------------------------------------------------------------
install_apt_radio() {
	local p want=()
	for p in direwolf fldigi js8call esptool; do  # esptool: offline flashing of the ESP32 mesh boards
		if pkg_available "$p"; then
			want+=("$p")
		else
			skipped "$p" "no package for this system"
		fi
	done
	((${#want[@]})) && apt_install "${want[@]}"
	return 0
}

install_pat() {
	local sha=${PAT_SHA256[$ARCH]:-} name deb
	if [[ -z $sha ]]; then
		skipped "Pat" "no Pat package for architecture $ARCH"
		return 0
	fi
	name="pat_${PAT_VERSION}_linux_${ARCH}.deb"
	deb="$(fetch "https://github.com/la5nta/pat/releases/download/v$PAT_VERSION/$name" "$sha" "$name")"
	install_deb "$deb"
	note "Pat (Winlink): run 'pat configure' as your own user, set your call sign, and change http_addr to localhost:5000 (Pat's default port 8080 is used by Kiwix here). Then 'pat http' and open http://localhost:5000."
}

install_mesh_client() {
	local sha=${MESH_SHA256[$ARCH]:-} name deb
	if [[ -z $sha ]]; then
		skipped "Mesh-client" "no Mesh-client package for architecture $ARCH"
		return 0
	fi
	name="mesh-client_${MESH_VERSION}_${ARCH}.deb"
	deb="$(fetch "https://github.com/Colorado-Mesh/mesh-client/releases/download/v$MESH_VERSION/$name" "$sha" "$name")"
	install_deb "$deb"
}

install_chirp() {
	local file=$CHIRP_FILE f fuse
	if [[ -z $file ]]; then
		# Newest file someone placed in the store (by hand or from the hub).
		for f in "$STORE"/radio/chirp/chirp-*-py3-none-any.whl "$STORE"/radio/chirp/Chirp-next-*-x86_64.AppImage; do
			[[ -f $f ]] || continue
			if [[ $f == *.AppImage && $ARCH != amd64 ]]; then continue; fi
			if [[ -z $file || $(basename "$f") > $(basename "$file") ]]; then file=$f; fi
		done
	fi
	if [[ -z $file ]]; then
		skipped "CHIRP" "its download site blocks scripts; see 'CHIRP' in deploy/INSTALL.md to add it by hand"
		note "CHIRP: download it from https://archive.chirpmyradio.com/chirp_next/ (newest folder). On this $ARCH machine get $([[ $ARCH == amd64 ]] && echo 'Chirp-next-DATE-x86_64.AppImage' || echo 'chirp-DATE-py3-none-any.whl'), put it in $STORE/radio/chirp/ and re-run this installer."
		return 0
	fi
	if [[ ! -f $file ]] && ((!DRY)); then
		echo "   CHIRP file not found: $file" >&2
		return 1
	fi
	case $file in
	*.whl)
		apt_install python3-wxgtk4.0 python3-yattag python3-serial pipx
		# A system-wide pipx install so every desktop user gets the 'chirp' command.
		run env PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin PIPX_MAN_DIR=/usr/local/share/man \
			pipx install --force --system-site-packages "$file"
		;;
	*.AppImage)
		if [[ $ARCH != amd64 ]]; then
			echo "   The CHIRP AppImage is only for x86_64 computers; use the .whl file on $ARCH." >&2
			return 1
		fi
		fuse=libfuse2t64
		pkg_available "$fuse" || fuse=libfuse2
		apt_install "$fuse"
		run install -D -m 0755 "$file" /opt/chirp/chirp.AppImage
		run ln -sf /opt/chirp/chirp.AppImage /usr/local/bin/chirp
		;;
	*)
		echo "   CHIRP file must be a .whl or .AppImage: $file" >&2
		return 1
		;;
	esac
	note "CHIRP: start it with 'chirp' from a desktop session."
}

add_radio_groups() {
	local user=${SUDO_USER:-} g groups=()
	if [[ -z $user || $user == root ]]; then
		info "Run with sudo from your own account to get serial-port access for radios added automatically."
		return 0
	fi
	for g in dialout audio plugdev; do
		getent group "$g" >/dev/null && groups+=("$g")
	done
	((${#groups[@]})) || return 0
	run usermod -aG "$(
		IFS=,
		echo "${groups[*]}"
	)" "$user"
	note "User $user can use radio cables and USB mesh boards (groups ${groups[*]}) after logging out and back in."
}

if ((IS_NODE)); then
	step "Install radio and mesh programs"
	if ((RADIO)); then
		optional "Radio programs from the package archive" install_apt_radio
		optional "Pat (Winlink)" install_pat
		optional "Mesh-client" install_mesh_client
		optional "CHIRP" install_chirp
		optional "Radio device access" add_radio_groups
	else
		skipped "Radio and mesh programs" "--no-radio was given"
	fi
fi

# ---------------------------------------------------------------------------------------------
# Step 8: offline map viewer (nodes)
# ---------------------------------------------------------------------------------------------
install_maps() {
	local dir="$STORE/web/maps" f
	f="$(fetch "https://cdn.jsdelivr.net/npm/maplibre-gl@$MAPLIBRE_VERSION/dist/maplibre-gl.js" "$MAPLIBRE_JS_SHA256" "maplibre-gl-$MAPLIBRE_VERSION.js")"
	run install -m 0644 -o bunkr -g bunkr "$f" "$dir/maplibre-gl.js"
	f="$(fetch "https://cdn.jsdelivr.net/npm/maplibre-gl@$MAPLIBRE_VERSION/dist/maplibre-gl.css" "$MAPLIBRE_CSS_SHA256" "maplibre-gl-$MAPLIBRE_VERSION.css")"
	run install -m 0644 -o bunkr -g bunkr "$f" "$dir/maplibre-gl.css"
	f="$(fetch "https://cdn.jsdelivr.net/npm/pmtiles@$PMTILES_VERSION/dist/pmtiles.js" "$PMTILES_JS_SHA256" "pmtiles-$PMTILES_VERSION.js")"
	run install -m 0644 -o bunkr -g bunkr "$f" "$dir/pmtiles.js"
	local entry rel sum font
	for entry in "${FONT_FILES[@]}"; do
		rel=${entry% *}
		sum=${entry##* }
		font=${rel%/*}
		[[ $font == "$rel" ]] && font=""
		run install -d -o bunkr -g bunkr -m 0755 "$dir/fonts${font:+/$font}"
		f="$(fetch "https://cdn.jsdelivr.net/gh/protomaps/basemaps-assets@$FONTS_COMMIT/fonts/${rel// /%20}" "$sum" "font-${rel//[\/ ]/_}")"
		run install -m 0644 -o bunkr -g bunkr "$f" "$dir/fonts/$rel"
	done
	put_file "$dir/index.html" 0644 bunkr:bunkr brief <"$SRC_DIR/deploy/pi/maps/index.html"
	put_file "$dir/VERSIONS.txt" 0644 bunkr:bunkr <<EOF
maplibre-gl $MAPLIBRE_VERSION (BSD-3-Clause) sha256 js $MAPLIBRE_JS_SHA256 css $MAPLIBRE_CSS_SHA256
Noto Sans label glyphs from protomaps/basemaps-assets@$FONTS_COMMIT (SIL Open Font License, fonts/OFL.txt)
pmtiles $PMTILES_VERSION (BSD-3-Clause) sha256 $PMTILES_JS_SHA256
EOF
}
if ((IS_NODE)); then
	step "Install the offline map viewer"
	optional "Map viewer" install_maps
fi

# ---------------------------------------------------------------------------------------------
# Step 9: summary
# ---------------------------------------------------------------------------------------------
mapfile -t ADDRS < <(ipv4_addrs)
[[ $ROLE == pi ]] && ((HOTSPOT)) && [[ " ${ADDRS[*]} " != *" $HOTSPOT_ADDR "* ]] && ADDRS+=("$HOTSPOT_ADDR")
((${#ADDRS[@]})) || ADDRS=(127.0.0.1)

printf '\n================================================================\n'
if ((DRY)); then
	printf ' DRY RUN finished: nothing was changed. Role: %s\n' "$ROLE"
else
	printf ' BUNKR is installed. Role: %s\n' "$ROLE"
fi
printf '================================================================\n'
echo " Open BUNKR in a browser:"
for a in "${ADDRS[@]}"; do echo "   http://$a:$BUNKR_PORT"; done
[[ $ROLE == pi ]] && echo "   http://$NEW_HOSTNAME.local:$BUNKR_PORT"
if ((KIWIX_OK)); then
	echo " Offline libraries (once a .zim file is in $STORE/library):"
	for a in "${ADDRS[@]}"; do echo "   http://$a:$KIWIX_PORT"; done
fi
if ((IS_NODE)); then
	echo " Offline maps (once a .pmtiles file is in $STORE/maps):"
	echo "   http://${ADDRS[0]}:$BUNKR_PORT/store/web/maps/"
fi
if [[ $ROLE == laptop && ${#ALLOW_NETS[@]} -eq 0 ]]; then
	echo " The firewall only allows this laptop itself. To use it from other devices, re-run with"
	echo "   --allow-subnet 192.168.X.0/24 (your home network)."
fi
if [[ -s $LOG_DIR/hotspot_new ]]; then
	echo
	echo " NEW Wi-Fi hotspot (shown only this once; also saved in $HOTSPOT_FILE):"
	sed 's/^/   /' "$LOG_DIR/hotspot_new"
fi
if [[ -s $LOG_DIR/notes ]]; then
	echo
	echo " Notes:"
	sed 's/^/   - /' "$LOG_DIR/notes"
fi
if [[ -s $LOG_DIR/skipped ]]; then
	echo
	echo " Skipped:"
	sed 's/^/   - /' "$LOG_DIR/skipped"
fi
if [[ -s $LOG_DIR/warnings ]]; then
	echo
	echo " Warnings:"
	sed 's/^/   - /' "$LOG_DIR/warnings"
fi
echo
echo " Next steps:"
echo "   1. Create the first login:"
echo "        cd $APP_DIR && sudo -u bunkr python3 -m bunkr --db $DATA_DIR/bunkr.db user add YOURNAME --admin"
if ((IS_NODE)); then
	if [[ -z $HUB_URL ]]; then
		echo "   2. To sync from the hub, re-run this installer with --hub-url http://HUB:$BUNKR_PORT"
	elif ((PAIRED)); then
		echo "   2. Paired with $HUB_URL; syncing hourly. Run one now: sudo systemctl start bunkr-sync.service"
	else
		echo "   2. Pair with the hub, then switch on the hourly sync:"
		echo "        cd $APP_DIR && sudo python3 -m bunkr pair --hub $HUB_URL --token-file $TOKEN_FILE"
		echo "        sudo chown bunkr:bunkr $TOKEN_FILE && sudo chmod 600 $TOKEN_FILE"
		echo "        sudo systemctl enable --now bunkr-sync.timer"
	fi
fi
echo "   Full guide: $APP_DIR/deploy/INSTALL.md"
