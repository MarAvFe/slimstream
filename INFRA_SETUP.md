# slimstream — Infra Commissioning Guide

Human, click-and-type steps to stand up the VM this pipeline runs on. Nothing here is application code — that starts after this guide and Phase 0 of `IMPLEMENTATION_GUIDE.md` are both done. **Do this before Phase 0**, since Phase 0's tests need to run on the real VM, not your laptop (session persistence, A1, is specifically a VM-reboot test).

Provider: **DigitalOcean**, per spec 1.13 (free inbound / cheap outbound fits this pipeline's traffic shape — large originals flow in for free, only small compressed copies flow out).

---

## 0. Prerequisites (5 min)

- A DigitalOcean account with a payment method attached.
- A paid Mega account (spec 1.13 assumes the Lite plan: 750GB storage / 12TB transfer) — sign up / confirm plan at mega.nz if not already done.
- An SSH key pair on your machine. If you don't have one:
  ```bash
  ssh-keygen -t ed25519 -C "slimstream" -f ~/.ssh/slimstream_do
  ```
  (ed25519, not RSA — faster and preferred by DO's own docs.)

---

## 1. DigitalOcean account setup (human, browser)

1. Log in at cloud.digitalocean.com
2. **Billing** → confirm a payment method is attached. Droplets bill per-second; a small box left running costs pennies/day but needs a valid card regardless.
3. **Settings → Security → SSH Keys** → add the public key:
   ```bash
   cat ~/.ssh/slimstream_do.pub
   ```
   Paste it in, name it `slimstream`.
4. (Optional but recommended) **API → Generate New Token** → name it `slimstream-provisioning`, scope: full access, no expiry needed for personal use. Save the token somewhere safe (password manager) — DO shows it once. This lets you provision from the CLI instead of clicking through the UI every time.

---

## 2. Install `doctl` locally (5 min)

```bash
# Linux
cd ~/Downloads
curl -sL https://github.com/digitalocean/doctl/releases/latest/download/doctl-$(curl -s https://api.github.com/repos/digitalocean/doctl/releases/latest | grep tag_name | cut -d '"' -f4 | tr -d v)-linux-amd64.tar.gz -o doctl.tar.gz
tar xf doctl.tar.gz
sudo mv doctl /usr/local/bin

doctl auth init   # paste the API token from step 1.4
doctl account get # sanity check — should print your account
```

---

## 3. Reserve a firewall + SSH key reference (CLI)

```bash
# confirm the key you uploaded is visible, note its ID
doctl compute ssh-key list

# create a cloud firewall: inbound SSH only, all outbound allowed
doctl compute firewall create \
  --name slimstream-fw \
  --inbound-rules "protocol:tcp,ports:22,address:0.0.0.0/0,address:::/0" \
  --outbound-rules "protocol:tcp,ports:all,address:0.0.0.0/0,address:::/0" \
  --outbound-rules "protocol:udp,ports:all,address:0.0.0.0/0,address:::/0"
```

If your home/office IP is static, restrict the inbound rule's `address:` to it instead of `0.0.0.0/0` — narrower is better for a box that will hold Mega session credentials.

---

## 4. Cloud-init: bake in hardening + dependencies on first boot

This is the one artifact this guide produces. Save it as `cloud-init.yaml` — it creates a non-root sudo user, locks down SSH, and installs everything Phase 0 needs, all before you ever log in manually.

```yaml
#cloud-config
users:
  - name: slimstream
    groups: sudo
    shell: /bin/bash
    sudo: ['ALL=(ALL) NOPASSWD:ALL']
    ssh_authorized_keys:
      - <PASTE CONTENTS OF ~/.ssh/slimstream_do.pub HERE>

package_update: true
package_upgrade: true

packages:
  - ffmpeg
  - imagemagick
  - libheif-examples   # gives heif-convert, for A4 (Pixel HEIC handling)
  - python3
  - python3-pip
  - python3-venv
  - sqlite3
  - git
  - ufw

write_files:
  - path: /etc/ssh/sshd_config.d/99-slimstream.conf
    content: |
      PermitRootLogin prohibit-password
      PasswordAuthentication no

runcmd:
  - systemctl restart sshd
  - ufw allow OpenSSH
  - ufw --force enable
  # MEGAcmd isn't in Ubuntu's default repos - install from MEGA's own package
  - wget -q -O /tmp/megacmd.deb https://mega.nz/linux/repo/xUbuntu_24.04/amd64/megacmd-xUbuntu_24.04_amd64.deb
  - apt-get install -y /tmp/megacmd.deb
  - rm /tmp/megacmd.deb
```

**Before using this file:** replace the `ssh_authorized_keys` placeholder with your actual public key contents (the full line: type, base64 blob, and comment — don't trim it), and double-check the MEGAcmd `.deb` URL against [mega.nz/cmd](https://mega.nz/cmd) — MEGA occasionally revises the package path per Ubuntu release, and a stale URL fails silently inside `runcmd` (check `/var/log/cloud-init-output.log` after boot if MEGAcmd isn't there).

**Also check for stray non-ASCII characters** (smart quotes, em dashes, curly apostrophes) if you paste this block from a rendered doc or chat — cloud-init's YAML parser can reject the *entire* file over a single invalid character (e.g. `unacceptable character #x0080`), silently falling back to DigitalOcean's bare defaults with no `slimstream` user and none of the packages installed, while `cloud-init status` still reports `done` and gives no obvious error. If SSH as `slimstream` gets refused but `ssh root@<ip>` works, this is the first thing to check:
```bash
ssh root@<DROPLET_IP> "grep -i 'unacceptable character\|Failed loading yaml' /var/log/cloud-init.log"
```
If that finds a hit, sanitize the local `cloud-init.yaml` (plain ASCII `-` instead of `—`, straight quotes instead of curly ones) and recreate the droplet — cloud-init only applies user-data on first boot, so a bad file can't be fixed by rebooting.

---

## 5. Create the droplet

```bash
doctl compute droplet create slimstream \
  --image ubuntu-24-04-x64 \
  --size s-2vcpu-2gb \
  --region nyc3 \
  --ssh-keys "$(doctl compute ssh-key list --format ID --no-header)" \
  --user-data-file ./cloud-init.yaml \
  --enable-monitoring \
  --wait

doctl compute droplet list   # note the public IP
```

Sizing rationale (spec 1.13): the bottleneck is CPU during transcode and scratch disk, not bandwidth or Mega quota — `s-2vcpu-2gb` (~$18/mo, check current pricing at the time you provision) is a reasonable start; scale up later if transcode is too slow, scale down or destroy-between-batches if idle most of the month. Pick `--region` closest to you for lower SSH/latency, it doesn't affect Mega throughput.

Assign your firewall to the droplet. `add-droplets` needs the firewall's **ID**, not its name (passing the name 404s) — look both IDs up explicitly rather than piping an unscoped `droplet list`, which would sweep in any other droplets on the account:

```bash
doctl compute firewall list --format ID,Name
doctl compute droplet list --format ID,Name

doctl compute firewall add-droplets <FIREWALL_ID> --droplet-ids <SLIMSTREAM_DROPLET_ID>

# verify:
doctl compute firewall get <FIREWALL_ID> --format Name,DropletIDs
```

---

## 6. First login and verification (human)

```bash
ssh -i ~/.ssh/slimstream_do slimstream@<DROPLET_IP>

# verify cloud-init finished and nothing errored
cloud-init status --wait
sudo tail -50 /var/log/cloud-init-output.log

# verify the toolchain
ffmpeg -version | head -1
convert -version | head -1   # Ubuntu 24.04's imagemagick package is IM6 - no unified `magick` binary
heif-convert --help 2>&1 | head -1
python3 --version
mega-version
```

If `mega-version` fails, cloud-init's MEGAcmd install step likely hit a stale `.deb` URL — reinstall manually:
```bash
wget -O /tmp/megacmd.deb https://mega.nz/linux/repo/xUbuntu_24.04/amd64/megacmd-xUbuntu_24.04_amd64.deb
sudo apt-get install -y /tmp/megacmd.deb
```

---

## 7. Mega login (human, once)

**`mega-login` (the scriptable command) does not prompt for a password.** Confirmed on this deployment — non-interactive mode requires `email password` as two literal arguments on the same line:
```
[cmd ERR  Extra args required in non-interactive mode. Usage: login [--auth-code=XXXX] email password | ...]
```
Typing the password as a CLI argument puts it in shell history and briefly in the process list (`ps` shows other users' full command lines) — exactly what we don't want for an account credential. Use the **interactive shell** instead, which does prompt securely:

```bash
mega-cmd
```
This drops you into a `MEGA CMD>` prompt. There, run (no `mega-` prefix inside the shell):
```
login your-mega-email@example.com
```
It will prompt for your password with input hidden, log in, and persist the session to the background `mega-cmd-server` daemon — the same daemon the scriptable `mega-*` commands talk to. Once logged in:
```
whoami
quit
```
`quit` only exits the interactive shell; the server keeps running and stays logged in (this is the persistence A1 tests below).

This login is exactly what Phase 0's **A1** tests: log in once here, then (after a `sudo reboot`) confirm the session survives without re-running login. Do that reboot test now, while you're already on the box — it's the cheapest possible moment to run it.

```bash
mega-whoami        # confirm logged in
sudo reboot
# wait, reconnect
ssh -i ~/.ssh/slimstream_do slimstream@<DROPLET_IP>
mega-ls             # if this works without re-login, A1 = confirmed
```

---

## 8. Directory layout on the VM

```bash
mkdir -p ~/slimstream/{scratch,logs}
git clone <your fork/repo URL> ~/slimstream/app   # once the repo exists
```

`scratch/` is Job A's download/transcode working directory (`SCRATCH_DIR` in config) — give it room; check `df -h` periodically, disk pressure is a documented failure mode (spec 1.12).

---

## 9. What you should have when this guide is done

- [ ] Droplet running, reachable only via SSH key (no password auth, root login disabled)
- [ ] Cloud firewall attached, inbound restricted to SSH
- [ ] ffmpeg, ImageMagick+libheif, Python 3, sqlite3, MEGAcmd all installed and version-checked
- [ ] Mega session logged in and confirmed to survive a reboot (A1 done)
- [ ] `~/slimstream/{scratch,logs}` created

**Next:** return to `IMPLEMENTATION_GUIDE.md` Phase 0 and run the remaining assumption tests (A2/A2b through A6) on this box, using a throwaway Mega test folder — not real photos.

---

## Cost note

Per-second billing means you don't have to leave this running 24/7 for testing — but Phase 6 of the implementation guide schedules Job A daily and Job B monthly via systemd timers, which does assume an always-on box for the real deployment. Destroying and recreating the droplet per batch (spec 1.13 mentions this as an option) trades a few minutes of provisioning latency per run for near-zero idle cost; not needed for MVP, worth revisiting once Job A/B are trusted and cost is measured for real.
