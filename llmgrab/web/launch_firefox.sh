#!/usr/bin/env bash
# Launch a dedicated Firefox profile that writes its TLS session keys to
# data/web/keys.log (NSS SSLKEYLOGFILE). Traffic is not modified.
#
# Capture packets in parallel (needs root, run it yourself, from a user with sudo
# privileges; `llmgrab tcpdump` prints the exact line):
#   sudo tcpdump -i <iface> -U -Z <this user> -w <project>/data/web/claude.pcap 'tcp port 443'
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WEB="$ROOT/data/web"
PROFILE="$WEB/firefox-profile"

umask 077
mkdir -p "$WEB" "$PROFILE"
chmod 700 "$ROOT/data" "$WEB" "$PROFILE"

# HTTP/3 (QUIC) off: Firefox falls back to HTTP/2 over TLS, which `llmgrab decode` handles.
cat > "$PROFILE/user.js" <<'EOF'
user_pref("network.http.http3.enable", false);
user_pref("browser.shell.checkDefaultBrowser", false);
EOF

IFACE="$(ip route get 1.1.1.1 | sed -n 's/.* dev \([^ ]*\).*/\1/p')"
echo "keys    -> $WEB/keys.log"
echo "capture -> sudo tcpdump -i $IFACE -U -Z $(id -un) -w $WEB/claude.pcap 'tcp port 443'"

# --new-instance: otherwise an already running Firefox takes the URL and ignores SSLKEYLOGFILE.
SSLKEYLOGFILE="$WEB/keys.log" exec firefox --new-instance --profile "$PROFILE" https://claude.ai
