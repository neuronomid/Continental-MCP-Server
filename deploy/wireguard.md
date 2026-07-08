# Overlay networking (decide this FIRST — mcp_plan.md §8, Phase 0 heads-up)

The research engine exposes **nothing public**. REST + MCP bind to a private
overlay IP; only the trading bot machine and your agent client join the overlay.

## Option A — Tailscale (simplest)
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
tailscale ip -4          # → e.g. 100.64.0.1  → set PMRE_SERVING_HOST to this
```

## Option B — WireGuard (manual)
```ini
# /etc/wireguard/wg0.conf on the VPS
[Interface]
Address = 100.64.0.1/24
PrivateKey = <vps-private-key>
ListenPort = 51820

[Peer]                     # the local trading-bot machine
PublicKey = <bot-public-key>
AllowedIPs = 100.64.0.2/32
```
```bash
sudo wg-quick up wg0
```

## Firewall (UFW default-deny)
```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 51820/udp            # WireGuard (skip for Tailscale)
sudo ufw allow in on tailscale0     # or: allow from 100.64.0.0/24
sudo ufw allow OpenSSH
sudo ufw enable
```

Then set in `.env`: `PMRE_SERVING_HOST=100.64.0.1`. REST/MCP will never listen on
a public interface. No Polymarket credentials of any kind live on the VPS.
