# media-automation
docker compose file containing the services required for setting up my personal media automation

# updating containers
```
docker compose pull
docker compose up --force-recreate -d
docker image prune -f
```

# restarting containers
if you've disabled a service by commenting out its include line but haven't removed the old container, `docker compose up -d` will restart only the enabled services without touching the disabled one:

```
docker compose up -d
```

# refreshing proton vpn wireguard configuration
expires yearly, november 26th ish.
to refresh, go to the proton vpn wireguard configuration page [here](https://account.proton.me/u/0/vpn/WireGuard). This link is also available in the docker compose file.
click the existing configuration
click extend to push its expiration back another year
run:
```
docker compose down
docker compose up --force-recreate -d
```

don't forget to also click the link inside the tailscale logs to reactivate tailscale!

# caddy reverse proxy

caddy acts as a reverse proxy so all services are accessed through a single port (80) instead of each service exposing its own port. this means only one Windows Firewall rule is needed for LAN access, and it sets the foundation for a future Cloudflare Tunnel setup.

## how it works
- caddy listens on port 80 and routes requests by subdomain (e.g., `http://komga.local` → komga container)
- services behind caddy don't publish their own ports — caddy reaches them via Docker's internal DNS
- the Caddyfile is at `C:\MEDIA_AUTOMATION\Caddy\Caddyfile`

## adding a new service to caddy
1. add a block to the Caddyfile:
   ```
   http://myservice.local {
       reverse_proxy container-name:port
   }
   ```
2. reload caddy:
   ```
   docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
   ```
3. add DNS so devices can resolve the subdomain:
   - **router**: add a DNS Host Mapping entry (`myservice.local` → `192.168.1.2`) under Advanced > IP Address > DNS Host Mapping. this covers devices using the router for DNS (e.g., phones).
   - **host PC**: run `.\scripts\sync-hosts.ps1` in an admin PowerShell. this parses domains from the Caddyfile and `LOCAL_STATIC_IP` from `.env`, then updates a managed section in the hosts file. needed because this PC uses Cloudflare DNS (`1.1.1.1`) instead of the router.
4. remove the `ports:` section from the service's compose file if it was previously exposed directly

## windows firewall
caddy requires an inbound firewall rule for LAN access. this was created with:
```powershell
New-NetFirewallRule -DisplayName "Caddy Reverse Proxy" -Direction Inbound -LocalPort 80,443 -Protocol TCP -Action Allow -Profile Private
```
this only needs to be done once — individual services behind caddy don't need their own firewall rules.

## why plain HTTP (not HTTPS)
caddy's `tls internal` generates certificates from its own CA inside the container. devices on the LAN don't trust this CA, causing connection timeouts (especially on Android). plain HTTP is fine for local traffic that never leaves the LAN. when a Cloudflare Tunnel is set up later, Cloudflare will handle HTTPS with real certificates.

# listing ports

to see which ports are in use across all services:

```
& "C:\Program Files\Git\bin\bash.exe" scripts/list-ports.sh
```