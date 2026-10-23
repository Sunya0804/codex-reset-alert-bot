#!/usr/bin/env bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ "$(id -u)" -eq 0 || "$PWD" != "$HOME/codex_reset_alert_bot" ]]; then
  echo 'Run as the Ubuntu login user from ~/codex_reset_alert_bot.' >&2
  exit 1
fi
if [[ ! -x .venv/bin/python || ! -f .env ]]; then
  echo 'Run install-linux.sh and configure .env first.' >&2
  exit 1
fi
export PATH="$HOME/.local/bin:$PATH"
codex login status
.venv/bin/python -B - <<'PY'
from dotenv import dotenv_values
v = dotenv_values('.env')
has = lambda name: bool((v.get(name) or '').strip())
if not (has('TELEGRAM_BOT_TOKEN') or has('DISCORD_WEBHOOK_URL')):
    raise SystemExit('Set TELEGRAM_BOT_TOKEN or DISCORD_WEBHOOK_URL in .env first.')
if has('TELEGRAM_BOT_TOKEN') and not has('TELEGRAM_CHAT_ID'):
    raise SystemExit('Missing TELEGRAM_CHAT_ID; configure .env first.')
if v.get('CODEX_COMMAND') and ':' in v['CODEX_COMMAND']:
    raise SystemExit('Clear the Windows CODEX_COMMAND in .env before Linux deployment.')
PY
mkdir -p "$HOME/.config/systemd/user"
install -m 644 deploy/codex-reset-alert.service "$HOME/.config/systemd/user/codex-reset-alert.service"
systemctl --user daemon-reload
systemd-analyze --user verify "$HOME/.config/systemd/user/codex-reset-alert.service"
# Keep this user's service manager alive after SSH logout and at boot.
sudo loginctl enable-linger "$(id -un)"
systemctl --user enable codex-reset-alert.service
systemctl --user restart codex-reset-alert.service
systemctl --user --no-pager status codex-reset-alert.service
echo 'The restart-success notice is sent only after a successful X scan and Codex check.'
echo 'Logs: journalctl --user -u codex-reset-alert.service -f'
