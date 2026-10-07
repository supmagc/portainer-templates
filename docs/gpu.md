# GPU (single Quadro P400)

One NVENC-capable GPU is shared by Emby, Jellyfin, Tdarr (`utilities` stack), stash
(`extras`) and the Dagu `trailer-fetch` / `backdrop-generate` jobs.

## 2026-10-07: Tdarr restart wedged the GPU

Watchtower recreated Tdarr mid-NVENC-encode. The kill left the GPU/driver context wedged
and every other GPU user (the two Dagu jobs) blocked inside the driver instead of failing.
The wedge is the likely cause, not confirmed: no Xid evidence was captured.

Mitigations in the repo:

- Tdarr `stop_grace_period: 2m`. This is a Compose setting, honoured by `docker stop` and
  `compose stop/down`, **not** by Watchtower.
- `WATCHTOWER_TIMEOUT=5m` on the Watchtower service. Watchtower's stop uses its own
  timeout (default 10s), not the container's `stop_grace_period`. It applies to every
  opted-in container.
- `timeout_sec: 7200` on the GPU steps of the two Dagu jobs, so a hang fails the run
  (alertable) instead of blocking.

Tdarr's Watchtower label is currently `true`. Emby and Jellyfin also use the GPU and are Watchtower-enabled. Whether SIGTERM actually
reaches Tdarr's ffmpeg children is unverified; test with `time docker stop -t 120 tdarr`
during an encode, then check `dmesg` and `nvidia-smi`.

## If it recurs

`dmesg | grep -i xid`. If `nvidia-smi` hangs or processes sit in D state
(`ps -eo pid,stat,cmd | awk '$2 ~ /^D/'`), only a host reboot recovers it. A timeout cannot
kill a D-state process; it only marks the DAG run failed.
