#!/bin/bash
# Keep femtomind.duckdns.org pointed at whatever public IP this host has.
# The IP is static today, but a rebuild or region move would silently break
# DNS -- and with it the TLS renewal -- if nothing kept them in sync.
TOKEN=$(cat "$HOME/.duckdns_token")
curl -fsS "https://www.duckdns.org/update?domains=femtomind&token=${TOKEN}&ip=" -o /tmp/duckdns.last 2>/dev/null
