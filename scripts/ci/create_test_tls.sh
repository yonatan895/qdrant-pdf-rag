#!/bin/sh
# CI-only private CA; never installed by a production deployment.
set -eu
[ "$#" -eq 2 ] || { echo "usage: $0 <directory> <dns-name>" >&2; exit 2; }
TLS_DIR="$1"
TLS_NAME="$2"
case "$TLS_NAME" in ''|*[!a-z0-9.-]*) echo "invalid TLS DNS name" >&2; exit 2 ;; esac
umask 077
mkdir -p "$TLS_DIR"
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj '/CN=Rehearsal CA' \
    -addext 'basicConstraints=critical,CA:TRUE' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' \
    -addext 'subjectKeyIdentifier=hash' \
    -keyout "$TLS_DIR/ca.key" -out "$TLS_DIR/ca.crt" 2>/dev/null
openssl req -new -newkey rsa:2048 -nodes -subj "/CN=$TLS_NAME" \
    -keyout "$TLS_DIR/tls.key" -out "$TLS_DIR/tls.csr" 2>/dev/null
cat > "$TLS_DIR/extensions" <<EOF
subjectAltName=DNS:$TLS_NAME
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
EOF
openssl x509 -req -in "$TLS_DIR/tls.csr" -CA "$TLS_DIR/ca.crt" \
    -CAkey "$TLS_DIR/ca.key" -CAcreateserial -days 2 \
    -extfile "$TLS_DIR/extensions" -out "$TLS_DIR/tls.crt" 2>/dev/null
openssl verify -x509_strict -CAfile "$TLS_DIR/ca.crt" "$TLS_DIR/tls.crt"
# Preserve the runner's ordinary roots as required by SSL_CERT_FILE.
cat /etc/ssl/certs/ca-certificates.crt "$TLS_DIR/ca.crt" > "$TLS_DIR/ca-bundle.crt"
