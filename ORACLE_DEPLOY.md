# Oracle VM + GitHub automatic deployment

This folder is an **extension** of the working Glenn/4F bot. The Discord commands,
application emojis, role pings, report, close, reopen, and SQLite format are unchanged.
The existing Nexon bot can remain in its own directory/service.

## Deployment model

Windows PC (edit and `git push`) → private GitHub repository → Oracle VM checks GitHub
~every 2 minutes → runs Python tests → restarts only the guild raid bot.

This setup does **not** require N100, a website, opening any new Oracle inbound ports,
or giving GitHub Actions SSH access to Oracle.

**Assumptions:** Oracle Cloud **Compute VM** with a modern Linux distribution,
SSH access, `systemd --user`, Git, and Python **3.10+**. For OCI Ubuntu images
the usual SSH account is `ubuntu`; for Oracle Linux images it is `opc`.
For Oracle Functions, managed hosting, or an OS without `systemd --user`, these
instructions must be adapted.

## 1. Put the project into GitHub from Windows

Create a **private, empty GitHub repository** (don't add README/license/gitignore
in the GitHub UI). On Windows, open a terminal in the folder containing `bot.py`.
Keep your real `.env` and `data/recruitments.sqlite3` on your machine; they must not
appear in the Git repository.

```powershell
git init
git branch -M main
git add .
git status --short
git commit -m "Initial raid scheduling bot"
git remote add origin git@github.com:YOUR_USERNAME/YOUR_REPOSITORY.git
git push -u origin main
```

If you prefer GitHub Desktop, you can publish the folder there as a **private**
repository instead. Verify `.env`, `data/`, and `.venv/` are not included.
The existing `.gitignore` in this project excludes all three.

## 2. Log in to your Oracle VM

From Windows PowerShell:

```powershell
ssh -i "C:\path\to\oracle_key.key" ubuntu@YOUR_ORACLE_PUBLIC_IP
```

Use `opc@...` instead if your VM is running Oracle Linux rather than Ubuntu.
On the VM, check `cat /etc/os-release` and `python3 --version`.

For **Ubuntu**, install required tools:

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip
```

For **Oracle Linux**, the package manager and Python package names differ. Install
Git, Python 3.10+ and venv support with `dnf` as appropriate for your image.

## 3. Give Oracle read-only access to the private repository

On the **Oracle VM**, as your normal SSH user, run:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/raidbot_github -C "oracle-raidbot" -N ""
cat ~/.ssh/raidbot_github.pub
```

Copy **only the PUBLIC key** printed by `cat`. On GitHub navigate to:
**Repository → Settings → Deploy keys → Add deploy key**. Paste the public key;
leave **Allow write access OFF**.

On Oracle, create a separate host alias so this key won't interfere with your
other bot or existing GitHub access:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github-raidbot
  HostName github.com
  User git
  IdentityFile ~/.ssh/raidbot_github
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config ~/.ssh/raidbot_github
```

Test `ssh -T git@github-raidbot`. Before accepting the first SSH host key,
verify its fingerprint against the GitHub documentation. GitHub may print a
successful-authentication message while returning exit code 1 (no shell access).

Then clone the project:

```bash
cd ~
git clone git@github-raidbot:YOUR_USERNAME/YOUR_REPOSITORY.git guild-raid-bot
cd ~/guild-raid-bot
```

## 4. Prepare the bot on Oracle

```bash
python3 --version     # requires Python 3.10 or newer
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
nano .env
.venv/bin/python -m unittest discover -s tests -v
```

In `.env` set the **production guild's IDs**, not those from your personal test
server: `GUILD_ID`, both `*_TEXT_CHANNEL_ID`, both `*_PARTICIPANT_ROLE_ID`,
`ORGANIZER_ROLE_ID`, plus the new bot's `DISCORD_TOKEN`.

**Leave `AUTO_CREATE_ENABLED=false` and `AUTO_CLOSE_ENABLED=false` initially.**
Production timers can be enabled *after* you verify `/파티 상태`, `/파티 이모지`,
and one manually created recruitment in the intended guild.

There is no need to upload application-owned weekday emojis again if you are
using the **same Discord application/bot**. For production, it's normally best
to start with a fresh SQLite database: the test-server database contains test
thread IDs. If you intentionally need those old records, back them up and move
that SQLite file separately, with the bot stopped.

**Stop the local Windows instance of this same Discord bot before starting Oracle.**
Don't run two copies of the same bot token or two independent automatic timers.
Your old Nexon bot uses a different token and can continue running.

## 5. Keep the new bot running after logout/reboot

From the same Oracle SSH account:

```bash
sudo loginctl enable-linger "$USER"
mkdir -p ~/.config/systemd/user
cp deploy/guild-raid-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now guild-raid-bot.service
systemctl --user status guild-raid-bot.service
journalctl --user -u guild-raid-bot.service -f
```

`loginctl enable-linger` allows user services to continue after SSH logout
and start at boot. `Ctrl+C` exits the log viewer, not the running bot.

## 6. Enable auto-updates from GitHub

```bash
cd ~/guild-raid-bot
cp deploy/guild-raid-bot-update.service deploy/guild-raid-bot-update.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now guild-raid-bot-update.timer
systemctl --user list-timers
```

Every ~2 minutes, Oracle checks the `main` branch. When a new commit is found,
`deploy/update.sh` fast-forwards, installs dependencies, runs compilation/tests,
then restarts **only this bot**. Updates do **not** overwrite `.env`, `data/`,
or `.venv/`. If tests fail it attempts to restore the previous code.

You can trigger one check immediately:

```bash
systemctl --user start guild-raid-bot-update.service
journalctl --user -u guild-raid-bot-update.service -n 60 --no-pager
```

## 7. Your daily development workflow

1. Edit source on Windows or in a GitHub branch.
2. Test locally: `python -m unittest discover -s tests -v`.
3. Commit and push to `main` (or merge a tested pull request into `main`).
4. Within ~2 minutes Oracle detects the update and restarts the bot.
5. Inspect logs on Oracle if the push introduces a bug.

**Important:** GitHub edits are not hot-patched into an already running Python
process. The updater must restart it to load new code. Avoid deploying immediately
before the weekly recruitment deadline or while moderators are making changes.

Useful commands:

```bash
systemctl --user status guild-raid-bot.service
journalctl --user -u guild-raid-bot.service -n 100 --no-pager
systemctl --user restart guild-raid-bot.service
systemctl --user stop guild-raid-bot.service
systemctl --user list-timers
```

## 8. Turn on the weekly jobs later

After live testing, edit `.env` **on Oracle**:

```dotenv
AUTO_CREATE_ENABLED=true
AUTO_CLOSE_ENABLED=true
# Illustrative KST settings, verify for your guild:
OPEN_DAYS_BEFORE=1
OPEN_TIME=18:00
CLOSE_DAYS_BEFORE=1
CLOSE_TIME=23:00
```

Restart with `systemctl --user restart guild-raid-bot.service`.
For each dungeon/week the database prevents normal duplicate creation even if
manual `/파티 생성` is used before the automatic opening job.

**Backup:** the actual roster is stored on Oracle at
`~/guild-raid-bot/data/recruitments.sqlite3`, not GitHub. Back up the database
regularly using SQLite's online backup API, or copy it while the bot is stopped
(plus any WAL sidecars). A GitHub push is not a database backup.

**Caveats:** auto-updates are convenient but not instantaneous; a GitHub
outage, a misconfigured deploy key, or a failed test can delay deployment.
Systemd keeps the bot running but Oracle VM or Discord/network outages can
still interrupt it. The bot's deadline recovery marks very late closures
for organizer review.
