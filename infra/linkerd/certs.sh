#!/usr/bin/env bash
# Generates the Linkerd trust anchor and issuer certificate once and keeps
# them in a Secret, then prints the directory they were written to.
set -euo pipefail
# shellcheck source=../lib.sh
. "$(dirname "$0")/../lib.sh"

dir="$1"
secret=heimdall-linkerd-certs

if ! k -n linkerd get secret "$secret" >/dev/null 2>&1; then
  log "linkerd: generating trust anchor and issuer certificates" >&2
  openssl ecparam -name prime256v1 -genkey -noout -out "$dir/ca.key"
  openssl req -x509 -new -key "$dir/ca.key" -sha256 -days 3650 \
    -subj "/CN=root.linkerd.cluster.local" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" -out "$dir/ca.crt"
  openssl ecparam -name prime256v1 -genkey -noout -out "$dir/issuer.key"
  openssl req -new -key "$dir/issuer.key" -subj "/CN=identity.linkerd.cluster.local" \
    -out "$dir/issuer.csr"
  printf 'basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n' >"$dir/ext"
  openssl x509 -req -in "$dir/issuer.csr" -CA "$dir/ca.crt" -CAkey "$dir/ca.key" \
    -CAcreateserial -days 3650 -sha256 -extfile "$dir/ext" -out "$dir/issuer.crt" 2>/dev/null
  # The root key is not needed again and is deliberately not stored.
  k -n linkerd create secret generic "$secret" --from-file=ca.crt="$dir/ca.crt" \
    --from-file=issuer.crt="$dir/issuer.crt" --from-file=issuer.key="$dir/issuer.key" >/dev/null
fi

for f in ca.crt issuer.crt issuer.key; do
  k -n linkerd get secret "$secret" -o "jsonpath={.data['${f//./\\.}']}" | base64 -d >"$dir/$f"
done
