# 🪞 Yggdrasil Website Mirror
A zero-friction, declarative Docker Compose setup to mirror standard HTTPS clearnet website to the Yggdrasil network for Alfis DNS.

No manual configuration files required. It spins up an Yggdrasil node and an Nginx reverse proxy sharing the same network namespace, handling SNI and Host headers automatically.

## 🚀 Quick Start
### 1. Generate an Yggdrasil Private Key
You need a static private key so your node's IPv6 address remains constant across reboots. Generate one instantly with this one-liner:

```bash
docker run --rm --entrypoint yggdrasil ghcr.io/yggdrasil-network/yggdrasil-go:latest -genconf | grep PrivateKey
```

(Save the output string, you will need it in the next step).

### 2. Run the Mirror
You don't need to clone this repository. Run the setup directly using the raw Compose file:
```bash
YGG_PRIV_KEY="<YOUR_GENERATED_PRIVATE_KEY>" \
YGG_PEERS="tcp://1.2.3.4:12345 tls://5.6.7.8:9012" \
TARGET_DOMAIN="asriyan.me" \
ADDITIONAL_DOMAINS="api.asriyan.me cdn.asriyan.me" \
docker compose -p my-website -f <(curl -fsSL https://raw.githubusercontent.com/ed-asriyan/yggdrasil-website-mirror/master/docker-compose.yml) up -d
```

| Variable | Required | Description |
| -------- | -------- | ----------- |
| `YGG_PRIV_KEY` | Yes | Your Yggdrasil private key. |
| `TARGET_DOMAIN` | Yes | The clearnet domain to proxy to (e.g., asriyan.me). Must support HTTPS. |
| `YGG_PEERS` | Yes | A space-separated list of Yggdrasil peers. *Find active public peers at [Yggdrasil Public Peers](https://publicpeers.neilalexander.dev).* |
| `ADDITIONAL_DOMAINS` | No | A space-separated list of additional domains to proxy to (e.g., API endpoints, CDN domains, auth servers) required by the main site. Must support HTTPS. Each entry can be `host` or `host:port` (e.g. `xftp.example.com:8443`) if the upstream listens on a non-standard port. |

*Also you can replace `my-website` with your preferred project name. It will be used as the container name prefix.*

Each mirrored domain gets its own listening port (`TARGET_DOMAIN` on 80, each `ADDITIONAL_DOMAINS` entry on 81, 82, ...). Responses are cached by Nginx (`proxy_cache`, 100MB, entries expire after 10 minutes) to reduce load and request rate on the origin servers. Nginx also rewrites occurrences of the real domain (with or without scheme, including bare `"host"`/`"host:port"` strings found in API/JSON responses) to point back to the mirror, so clients don't leak or connect directly to the original clearnet addresses.

### 3. Get your Yggdrasil IPv6 Address
Retrieve the IPv6 address assigned to your container's tun0 interface:
```bash
docker compose -p my-website logs yggdrasil | grep "Your IPv6 address is"
```

*Remember to replace `my-website` with your project name if you changed it in Step 2.*

### 4. Register in Alfis DNS (optional)
1. Open your [Alfis GUI](https://alfis.name).
2. Mine a new key if you haven't done it before
3. Register your target .ygg domain.
4. Create an AAAA record pointing to the IPv6 address obtained in Step 3.
