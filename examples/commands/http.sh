#!/bin/sh
# desc: Quick HTTP check of a few URLs (/sites does more: certificates, domains, alerts)
# phrases: http check, curl check
SITES="https://example.com https://example.org"

for url in $SITES; do
    out=$(curl -s -o /dev/null -m 10 -w '%{http_code} %{time_total}' "$url") || out="000 0"
    code=${out% *}
    secs=${out#* }
    case $code in
        2*|3*) mark="✅" ;;
        *) mark="❌" ;;
    esac
    echo "$mark $url  $code  ${secs}s"
done
