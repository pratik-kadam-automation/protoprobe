# Atreyo AG-221/702 Setup: Edge Collector

## 1. Base OS packages (opkg)

```sh
opkg update
opkg install python3 python3-pip python3-asyncio python3-sqlite3 \
             python3-logging kmod-usb-serial kmod-usb-acm \
             wireguard-tools ip-full
```

`python3-sqlite3` and `python3-logging` are often trimmed from the
minimal OpenWrt python3 build to save flash space -- confirm they're
actually present (`python3 -c "import sqlite3, logging"`) before
assuming buffer_manager.py will import cleanly.

## 2. Python dependencies (pip)

Pure-Python packages install fine over pip on MIPS. Anything with a
C extension needs the OpenWrt SDK cross-toolchain -- see the snap7
warning below.

```sh
pip3 install --no-cache-dir \
    pymodbus==3.* \
    paho-mqtt==1.6.* \
    asyncua
```

Leave `bacpypes3` out of this install unless you actually enable a
`bacnet_ip` polling group -- it's the heaviest dependency in the set
for 256MB of RAM (see `drivers/bacnet_driver.py` docstring).

### snap7 (S7comm) -- do this on a build machine, not the gateway

```sh
# on your dev machine, with the OpenWrt SDK for mips_24kc set up:
git clone https://github.com/gijzelaerr/python-snap7
# cross-compile libsnap7.so per snap7's build docs, targeting mips_24kc
# then copy libsnap7.so onto the gateway and:
pip3 install python-snap7 --no-deps
```

If this doesn't compile cleanly, use Option B in `config.json`
(Modbus TCP via the S7's `MB_SERVER` block) instead -- it needs
nothing beyond pymodbus, which is already installed above.

## 3. Deploy the code

```sh
mkdir -p /opt/edge-collector /etc/edge-collector/certs /overlay/edge-collector
scp -r drivers edge_collector.py buffer_manager.py root@<gateway-ip>:/opt/edge-collector/
scp config.json root@<gateway-ip>:/etc/edge-collector/
# copy your MQTT/AWS IoT certs into /etc/edge-collector/certs/
```

## 4. procd service

```sh
scp etc/init.d/edge-collector root@<gateway-ip>:/etc/init.d/edge-collector
chmod +x /etc/init.d/edge-collector
/etc/init.d/edge-collector enable
/etc/init.d/edge-collector start
logread -f   # tail logs
```

## 5. WireGuard remote access (for TIA Portal / GX Works over LTE)

```sh
opkg install wireguard-tools luci-proto-wireguard
uci set network.wg0=interface
uci set network.wg0.proto='wireguard'
uci set network.wg0.private_key='<gateway-private-key>'
uci add_list network.wg0.addresses='10.10.0.2/24'
uci commit network

uci add network wireguard_wg0
uci set network.@wireguard_wg0[-1].public_key='<engineer-laptop-public-key>'
uci add_list network.@wireguard_wg0[-1].allowed_ips='10.10.0.1/32'
uci set network.@wireguard_wg0[-1].endpoint_host='<vpn-server-ip>'
uci set network.@wireguard_wg0[-1].endpoint_port='51820'
uci set network.@wireguard_wg0[-1].persistent_keepalive='25'
uci commit network

ifup wg0
```

Then route the PLC's LAN subnet across the tunnel so TIA Portal/GX
Works on the engineer's laptop can reach it directly:

```sh
uci add network route
uci set network.@route[-1].interface='wg0'
uci set network.@route[-1].target='192.168.1.0/24'   # PLC-side LAN
uci commit network
/etc/init.d/network reload
```

## 6. Firewall

Only open what's needed: WireGuard's UDP port inbound, everything
else stays default-deny.

```sh
uci add firewall rule
uci set firewall.@rule[-1].name='Allow-WireGuard'
uci set firewall.@rule[-1].src='wan'
uci set firewall.@rule[-1].proto='udp'
uci set firewall.@rule[-1].dest_port='51820'
uci set firewall.@rule[-1].target='ACCEPT'
uci commit firewall
/etc/init.d/firewall reload
```

Add the WireGuard interface to your LAN zone (or a dedicated `vpn`
zone forwarded to `lan`) via LuCI or `/etc/config/firewall` so
traffic actually reaches the PLC subnet, not just the router itself.

## 7. Sanity checks before leaving site

```sh
# confirm the service is respawning correctly
/etc/init.d/edge-collector stop && sleep 2 && logread | tail -20

# confirm buffer is actually writing (pull the LTE SIM briefly, watch pending_count grow)
sqlite3 /overlay/edge-collector/buffer.db "SELECT COUNT(*) FROM outbox;"

# confirm it drains once network is back
sqlite3 /overlay/edge-collector/buffer.db "SELECT COUNT(*) FROM outbox;"  # should fall to 0
```
