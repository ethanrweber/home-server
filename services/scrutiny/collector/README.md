# scrutiny collector

the scrutiny web ui runs in the stack ([scrutiny.yml](../scrutiny.yml)), but the disks are on a different machine. this folder holds the files for that machine:

| file | install as | purpose |
|---|---|---|
| [collector.yaml](collector.yaml) | `/opt/scrutiny/config/collector.yaml` | the collector settings: the web ui address and the smartctl options |
| [scrutiny-collector.service](scrutiny-collector.service) | `/etc/systemd/system/` | reads the SMART data of each disk and sends it to the web ui |
| [scrutiny-collector.timer](scrutiny-collector.timer) | `/etc/systemd/system/` | starts the collector each night at 02:00 |
| [scrutiny-healthcheck.service](scrutiny-healthcheck.service) | `/etc/systemd/system/` | starts the healthcheck script after each good collector run |
| [scrutiny-healthcheck.sh](scrutiny-healthcheck.sh) | `/opt/scrutiny/bin/` | pings healthchecks.io with the status of the disks |

the collector runs as the `scrutiny-svc` user, and it can only connect to the docker host. after a good run, the healthcheck script reads the status of each disk from scrutiny and pings the "drive health" check. healthchecks.io emails you if a disk fails, if a disk sends no data, or if no ping arrives.

## installation

the scrutiny containers must run before step 10.

1. on healthchecks.io, create a check named "drive health". set the period to 1 day and the grace time to 6 hours.
2. add the uuid of the check to `services/homepage/homepage.env` as `HOMEPAGE_VAR_HEALTHCHECKS_DRIVES_UUID`.
3. copy this folder to the machine with the disks, for example with `scp`. do the remaining steps in a root shell, in that copy.
4. install the dependencies. scrutiny needs smartctl 7 or later:
   ```
   apt install smartmontools jq curl
   ```
5. create the service account, and add it to the `disk` group:
   ```
   adduser --system scrutiny-svc --group --home /opt/scrutiny-svc
   usermod -aG disk scrutiny-svc
   ```
6. download the collector and verify its checksum. the version must be the same as the image tag in [scrutiny.yml](../scrutiny.yml). the checksum is the sha256 digest of the file on the release page:
   ```
   v=0.9.5
   curl -fL -o /tmp/scrutiny-collector-metrics "https://github.com/AnalogJ/scrutiny/releases/download/v$v/scrutiny-collector-metrics-linux-amd64"
   echo "a7745fe3d4282857be07089298a4fa29b1f34e50fc7cc1c054b2945dd9edc320  /tmp/scrutiny-collector-metrics" | sha256sum -c   # must show "OK"
   install -D -m 755 /tmp/scrutiny-collector-metrics /opt/scrutiny/bin/scrutiny-collector-metrics
   ```
7. install the files from this folder:
   ```
   install -D -m 644 collector.yaml /opt/scrutiny/config/collector.yaml
   install -m 755 scrutiny-healthcheck.sh /opt/scrutiny/bin/
   install -m 644 scrutiny-collector.service scrutiny-collector.timer scrutiny-healthcheck.service /etc/systemd/system/
   ```
8. create an empty, root-only file for the ping url:
   ```
   install -d -m 700 /etc/scrutiny
   install -m 600 /dev/null /etc/scrutiny/healthcheck.env
   ```
9. with an editor, add the ping url of the check to `/etc/scrutiny/healthcheck.env`. an editor keeps the url out of the shell history:
   ```
   HC_URL=https://hc-ping.com/<uuid>
   ```
10. run the collector once, and read its log:
    ```
    systemctl daemon-reload
    systemctl start scrutiny-collector.service
    journalctl -u scrutiny-collector -u scrutiny-healthcheck -n 50
    ```
11. make sure that the scrutiny ui shows each disk as passed, and that the check on healthchecks.io is up. if a seagate disk shows as failed, read the comment about attribute 188 in [collector.yaml](collector.yaml).
12. enable the timer:
    ```
    systemctl enable --now scrutiny-collector.timer
    systemctl list-timers 'scrutiny*'
    ```

## alerts

if a disk has a problem, the email from healthchecks.io shows the ping body. it has one line for each problem disk:

- `device_status=1`: a SMART attribute is past the threshold that the disk itself sets.
- `device_status=2`: a SMART attribute is past a scrutiny threshold. scrutiny takes its thresholds from the backblaze failure statistics.
- `device_status=3`: both.
- `stale`: scrutiny has no data from the disk in the last 12 hours, for example because the disk is gone.

scrutiny keeps a failed status after the problem goes away. if you remove or replace a disk, archive it in the scrutiny ui. the healthcheck script and the homepage tile ignore archived disks.

## update

update the web image and the collector together, to the same version.

1. read the release notes of the new version.
2. change the image tag in [scrutiny.yml](../scrutiny.yml), and apply it with `docker compose up -d`.
3. on the machine with the disks, do step 6 again with the new version and its digest.
