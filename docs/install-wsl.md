# Running the Harmony server on Windows (WSL)

Windows users can run the **Harmony server** — the always-on hub the web app,
the phone app and other Harmony instances talk to — inside WSL 2 (Windows
Subsystem for Linux). You then use Harmony from any browser at
`http://localhost:8080`, or from your phone on the same network.

This guide covers the `.deb` server package. It doesn't cover the GTK desktop app.

## 1. Install WSL with Ubuntu

In **PowerShell as Administrator**:

```powershell
wsl --install -d Ubuntu
```

Restart if asked, open **Ubuntu** from the Start menu, and create your Linux user.
You need Windows 11, or Windows 10 22H2 with a current WSL (`wsl --update`).

Harmony runs as a systemd service. Recent Ubuntu images on WSL enable systemd by
default; check it:

```bash
systemctl is-system-running   # anything except "offline" is fine
```

If it says `offline`, add this to `/etc/wsl.conf` inside Ubuntu, then run
`wsl --shutdown` in PowerShell and reopen Ubuntu:

```ini
[boot]
systemd=true
```

## 2. Install the server package

Download `harmony-server_<version>_all.deb` from the
[latest release](https://github.com/marthofdoom/harmony/releases/latest), then
inside Ubuntu:

```bash
sudo apt update
sudo apt install ./harmony-server_*_all.deb   # pulls in python3, ffmpeg, roc-toolkit-tools
systemctl status harmony                      # should say "active (running)"
```

A Windows Downloads folder is at `/mnt/c/Users/<you>/Downloads/` inside Ubuntu.

Open **http://localhost:8080** in your Windows browser. That's Harmony's web app.
Set a **personal key** in Accounts before letting other devices or instances in.

## 3. Add your music

Your Windows music folder is reachable from WSL as
`/mnt/c/Users/<you>/Music`. In the web app, go to **Accounts → Library**, add that
path, and enable the library. Scanning a Windows folder from WSL is slower than a
native Linux disk. Files stay where they are; Harmony only reads them.

## 4. Reach it from your phone and network (and cast)

By default WSL sits behind NAT. Windows itself can reach `localhost:8080`, but
your phone, other Harmony instances and cast devices **can't reach the server**.
Casting needs the speaker or TV to fetch audio back from Harmony, so it won't work
in NAT mode either. On Windows 11 (22H2 or later), switch WSL to
**mirrored networking**:

1. Create or edit `%UserProfile%\.wslconfig` on Windows:

   ```ini
   [wsl2]
   networkingMode=mirrored
   ```

2. Allow inbound connections to WSL. Run this in **PowerShell as Administrator**:

   ```powershell
   Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
   ```

3. Run `wsl --shutdown`, then reopen Ubuntu.

The server is then reachable at your PC's LAN address, `http://<pc-ip>:8080`. If
Windows Defender Firewall prompts, allow it on **private** networks only.

Automatic discovery (mDNS) may still not cross into WSL. If the phone or desktop
app doesn't find the server, add it by address:

- **Desktop:** Preferences → Network → Harmony instances.
- **Phone:** the manual address field.

Windows 10 has no mirrored mode. There you can forward port 8080 with
`netsh interface portproxy`, which gives browser and phone access. Casting still
won't work from behind NAT.

## 5. Keep it running

WSL can shut a distro down once its last window closes. To keep Harmony up, add a
**Task Scheduler** task at logon that runs:

```text
wsl.exe -d Ubuntu --exec sleep infinity
```

Set it to run hidden, whether or not you're logged on. The `harmony` service
starts with the distro.

## Updating, logs, removal

```bash
sudo apt install ./harmony-server_<new-version>_all.deb   # update in place; settings are kept
journalctl -u harmony -f                                  # live log
sudo apt remove harmony-server                            # remove (data in /var/lib/harmony is kept)
```

Settings, the library index and credentials live in `/var/lib/harmony`.
Credentials go to a 0600 file there, because WSL has no system keyring.
