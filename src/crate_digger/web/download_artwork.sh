#!/usr/bin/env bash
set -euo pipefail

url="${1:?missing artwork URL}"
output_path="${2:?missing output path}"
timeout_seconds="${3:-8}"
max_bytes="${4:-8388608}"

tmp_path="${output_path}.tmp"
headers_path="${output_path}.headers"
chunk_path="${output_path}.chunk"
chunk_size=8192
url_host="${url#*://}"
url_host="${url_host%%/*}"
url_host="${url_host%%:*}"

cleanup() {
  rm -f "$tmp_path" "$headers_path" "$chunk_path"
}
trap cleanup EXIT

timeout_command=()
if command -v timeout >/dev/null 2>&1; then
  timeout_command=(timeout --kill-after=2s "${timeout_seconds}s")
fi

resolve_args=()
if [[ "$url" == https://* ]] && command -v getent >/dev/null 2>&1; then
  ipv4_address="$(
    getent ahostsv4 "$url_host" | awk '$2 == "STREAM" { print $1; exit }'
  )"
  if [[ -n "$ipv4_address" ]]; then
    resolve_args=(--resolve "${url_host}:443:${ipv4_address}")
  fi
fi

curl_common=(
  --location
  --fail
  --silent
  --show-error
  --noproxy
  "*"
  "${resolve_args[@]}"
  --connect-timeout 3
  --max-time "$timeout_seconds"
  --max-filesize "$max_bytes"
  --retry 1
  --retry-delay 0
  --user-agent "crate-digger/1.0"
)

if "${timeout_command[@]}" curl \
  "${curl_common[@]}" \
  --output "$tmp_path" \
  "$url"; then
  byte_count="$(wc -c < "$tmp_path" | tr -d ' ')"
  if [[ "$byte_count" != "0" ]] && (( byte_count <= max_bytes )); then
    mv "$tmp_path" "$output_path"
    trap - EXIT
    printf '%s\n' "application/octet-stream"
    exit 0
  fi
fi

rm -f "$tmp_path"

"${timeout_command[@]}" curl \
  "${curl_common[@]}" \
  --head \
  --dump-header "$headers_path" \
  --output /dev/null \
  "$url"

content_length="$(
  awk '
    BEGIN { IGNORECASE = 1 }
    /^content-length:/ {
      value = $0
      sub(/^[^:]*:[[:space:]]*/, "", value)
      sub(/\r$/, "", value)
      content_len = value
    }
    END { print content_len }
  ' "$headers_path"
)"

mime="$(
  awk '
    BEGIN { IGNORECASE = 1 }
    /^content-type:/ {
      value = $0
      sub(/^[^:]*:[[:space:]]*/, "", value)
      sub(/\r$/, "", value)
      mime = value
    }
    END { print mime }
  ' "$headers_path"
)"

if [[ ! "$content_length" =~ ^[0-9]+$ ]]; then
  exit 1
fi
if [[ "$content_length" == "0" ]] || (( content_length > max_bytes )); then
  exit 1
fi

: > "$tmp_path"
for ((start = 0; start < content_length; start += chunk_size)); do
  end=$((start + chunk_size - 1))
  if (( end >= content_length )); then
    end=$((content_length - 1))
  fi

  "${timeout_command[@]}" curl \
    "${curl_common[@]}" \
    --range "$start-$end" \
    --output "$chunk_path" \
    "$url"

  cat "$chunk_path" >> "$tmp_path"
  rm -f "$chunk_path"
done

byte_count="$(wc -c < "$tmp_path" | tr -d ' ')"
if [[ "$byte_count" != "$content_length" ]]; then
  exit 1
fi

mv "$tmp_path" "$output_path"
rm -f "$headers_path"
trap - EXIT

printf '%s\n' "$mime"
