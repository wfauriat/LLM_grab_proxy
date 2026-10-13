#!/usr/bin/env bash
# Launch a dedicated Firefox profile that writes its TLS session keys to
# browsergrab/captures/keys.log (NSS SSLKEYLOGFILE). Traffic is not modified.
#
# Capture packets in parallel (needs root, run it yourself):
#   sudo tcpdump -i <iface> -U -Z "$USER" -w browsergrab/captures/claude.pcap 'tcp port 443'
#   sudo tcpdump -i wlo1 -U -Z ai-user -w /home/ai-user/Documents/Sandbox/LLMgrab/browsergrab/captures/claude.pcap 'tcp port 443'
#   (to run from a user with sudo privileges)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CAPTURES="$HERE/captures"
PROFILE="$HERE/profile"

umask 077
mkdir -p "$CAPTURES" "$PROFILE"
chmod 700 "$CAPTURES" "$PROFILE"

# HTTP/3 (QUIC) off: Firefox falls back to HTTP/2 over TLS, which parse_pcap.py decodes.
cat > "$PROFILE/user.js" <<'EOF'
user_pref("network.http.http3.enable", false);
user_pref("browser.shell.checkDefaultBrowser", false);
EOF

IFACE="$(ip route get 1.1.1.1 | sed -n 's/.* dev \([^ ]*\).*/\1/p')"
echo "keys    -> $CAPTURES/keys.log"
echo "capture -> sudo tcpdump -i $IFACE -U -Z $USER -w $CAPTURES/claude.pcap 'tcp port 443'"

# --new-instance: otherwise an already running Firefox takes the URL and ignores SSLKEYLOGFILE.
SSLKEYLOGFILE="$CAPTURES/keys.log" exec firefox --new-instance --profile "$PROFILE" https://claude.ai
