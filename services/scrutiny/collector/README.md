# scrutiny collector

the scrutiny web ui runs in the stack ([scrutiny.yml](../scrutiny.yml)), but the disks are on a different machine. this folder holds the files for that machine. [install.sh](install.sh) installs them:

| file | installed as | purpose |
|---|---|---|
| [collector.yaml](collector.yaml) | `/opt/scrutiny/config/collector.yaml` | the collector settings: the web ui address and the smartctl options |
| [scrutiny-collector.service](scrutiny-collector.service) | `/etc/systemd/system/` | reads the SMART data of each disk and sends it to the web ui |
| [scrutiny-collector.timer](scrutiny-collector.timer) | `/etc/systemd/system/` | starts the collector each night at 02:00 |
| [scrutiny-healthcheck.service](scrutiny-healthcheck.service) | `/etc/systemd/system/` | starts the healthcheck script after each good collector run |
| [scrutiny-healthcheck.sh](scrutiny-healthcheck.sh) | `/opt/scrutiny/bin/` | pings healthchecks.io with the status of the disks |

the collector runs as the `scrutiny-svc` user, and it can only connect to the docker host. after a good run, the healthcheck script reads the status of each disk from scrutiny and pings the "drive health" check. healthchecks.io emails you if a disk fails, if a disk sends no data, or if no ping arrives.

## installation

the scrutiny containers must run before step 3.

1. on healthchecks.io, create a check named "drive health". set the period to 1 day and the grace time to 6 hours.
2. add the uuid of the check to `services/homepage/homepage.env` as `HOMEPAGE_VAR_HEALTHCHECKS_DRIVES_UUID`.
3. on the machine with the disks, run this command in a root shell. it asks for the ping url of the check:
   ```
   curl -fsSL https://raw.githubusercontent.com/ethanrweber/home-server/main/services/scrutiny/collector/install.sh | bash
   ```
4. make sure that the scrutiny ui shows each disk as passed, and that the check on healthchecks.io is up. if a seagate disk shows as failed, read the comment about attribute 188 in [collector.yaml](collector.yaml).

the script installs smartmontools and jq, creates the `scrutiny-svc` account, verifies the checksum of the collector, installs the files, and enables the timer. then it runs the collector once and shows the log. the ping url goes into `/etc/scrutiny/healthcheck.env`, which only root can read. to change the url, edit that file.

## alerts

if a disk has a problem, the email from healthchecks.io shows the ping body. it has one line for each problem disk:

- `device_status=1`: a SMART attribute is past the threshold that the disk itself sets.
- `device_status=2`: a SMART attribute is past a scrutiny threshold. scrutiny takes its thresholds from the backblaze failure statistics.
- `device_status=3`: both.
- `stale`: scrutiny has no data from the disk in the last 12 hours, for example because the disk is gone.

if scrutiny lists no disks at all, the body is `scrutiny lists no disks`.

scrutiny sets the status again on each collector run, so a status clears when the next run is clean. archive a disk in the scrutiny ui only when you remove it. the healthcheck script and the homepage tile ignore archived disks, so an archived disk sends no more alerts.

## update

update the web image and the collector together, to the same version.

1. read the release notes of the new version.
2. in one commit on main, change the image tag in [scrutiny.yml](../scrutiny.yml), and `VERSION` and `SHA256` in [install.sh](install.sh). `SHA256` is the sha256 digest of `scrutiny-collector-metrics-linux-amd64` on the release page.
3. apply the new image with `docker compose up -d`.
4. on the machine with the disks, run the command from step 3 of the installation again.
