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

# tailscale serve & funnel

some services have their own tailscale sidecar container for remote access. each sidecar extends a shared base template (`services/tailscale-sidecar.yml`) and uses a `serve-config.json` to configure routing.

## how it works
- each sidecar gets its own hostname on the tailnet (e.g., `komga.tail17ddbe.ts.net`)
- the service shares the sidecar's network namespace via `network_mode: service:<sidecar>`
- tailscale serve proxies HTTPS on port 443 to the service's internal port
- setting `AllowFunnel` to `true` in the serve config makes the service publicly accessible

## current funneled services
| service | url | serve config |
|---------|-----|-------------|
| komga | `https://komga.tail17ddbe.ts.net` | `%CONFIG_ROOT%\ts-komga\config\serve-config.json` |
| calibre-web-automated | `https://calibre.tail17ddbe.ts.net` | `%CONFIG_ROOT%\ts-calibre-web-automated\config\serve-config.json` |

the general pattern is `%CONFIG_ROOT%\ts-<service>\config\serve-config.json`. these are outside the repo since they live under `CONFIG_ROOT` alongside other service config data.

## adding a new service with a tailscale sidecar
1. create config and state directories: `C:\MEDIA_AUTOMATION\ts-<service>\config` and `C:\MEDIA_AUTOMATION\ts-<service>\state`
2. create a `serve-config.json` in the config directory (copy from an existing one and update the port)
3. add a sidecar to the service's compose file using `extends`:
   ```yaml
   ts-myservice:
     extends:
       file: ../tailscale-sidecar.yml
       service: tailscale-sidecar
     container_name: ts-myservice
     hostname: myservice
     volumes:
       - ${CONFIG_ROOT}/ts-myservice/state:/var/lib/tailscale
       - ${CONFIG_ROOT}/ts-myservice/config:/config
   ```
4. set the service's `network_mode: service:ts-myservice` and add a `depends_on` with `condition: service_healthy`
5. enable the `funnel` node attribute in the [tailscale ACL policy](https://login.tailscale.com/admin/acls) if not already done (only needs to be done once for your tailscale account, _not_ once per service)

# listing ports

to see which ports are in use across all services:

```
& "C:\Program Files\Git\bin\bash.exe" scripts/list-ports.sh
```