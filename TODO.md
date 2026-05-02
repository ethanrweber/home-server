# Todo List

This is my future task list. It primarily serves as a way to document tasks that I'm interested in investigating possible solutions for.

My philosophy is that I don't need to find perfect solutions either. Since this is for my home lab / server, partial solutions may be acceptable, and I also just enjoy the learning process.

## Proxmox Monitoring

I want to display a simple web interface that syncs with my proxmox server. It would be nice if I could display any of the following details:

- usage/resource statistics and breakdown
  - "how much cpu/memory is currently being utilized by the server?"
  - "how much power is the node consuming?"
- proxmox node information
  - software updates:
    - "how many software updates are available?"
    - "when was the last time I updated?"
  - health checks:
    - "What does the SMART monitoring look like on all my drives?"
    - "Has SMART monitoring changed for any of my drives (in the last 24 hours / 1 week / 1 month / 1 year)?"
- VM information
  - "how many VMs exist on this node, and how many of those are running?"
  - combining this with the resource statistics detail - "how much of the computer is being utilized by each VM?" including cpu cores / memory allocations + the other listed resource statistics already mentioned

For context I already have `homepage` running in this VM's compose stack, which provides a nice web interface, and it does seem to offer a proxmox integration which may help in answering at least some of these questions. Even if solutions are available, I may also need to consider whether it is safe and secure to display some of this info given that the `homepage` service is visible on the public internet.

## Automatic Docker Updates

I've heard recently about a service called `watchtower`. It might be worth investigating to see if I can keep my docker compose stack updated automatically.

Off the top of my head, these questions seem relevant to investigate before deciding to adopt any automatic-update service:

- "which containers have updates available?"
- "is it safe to apply the updates?" -- not sure if that can be automated, probably requires manual investigation
  - maybe it would be more pertinent to ask "can I see what was changed before applying an update?"
