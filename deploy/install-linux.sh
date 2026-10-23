#!/usr/bin/env bash
# Run as the Ubuntu login user, from the transferred project directory.
set -euo pipefail
umask 077
if [[ "$(id -u)" -eq 0 ]]; then
  echo 'Run as the normal Ubuntu user, not root; sudo is used only for OS packages.' >&2
  exit 1
fi
if [[ "$(uname -s)" != Linux ]]; then
  echo 'This installer is for Ubuntu Linux.' >&2
  exit 1
fi
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ -d .venv/Scripts ]]; then
  echo 'A Windows .venv was copied. Transfer source files only and create a Linux venv.' >&2
  exit 1
fi
sudo apt-get update
sudo apt-get install -y python3 python3-venv ca-certificates nodejs npm
python3 -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
node -e 'if (Number(process.versions.node.split(".")[0]) < 16) process.exit(1)'
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
# Pin to the Codex version already verified with this bot; user-owned installation.
npm install --global --prefix "$HOME/.local" '@openai/codex@0.163.0'
export PATH="$HOME/.local/bin:$PATH"
codex --version
if [[ ! -f .env ]]; then
  cp -- .env.example .env
fi
chmod 600 .env
mkdir -p data
chmod 700 data
.venv/bin/python -B -m unittest -q
cat <<'NEXT'
Installation and local tests completed. Service has NOT been started.
Next:
  export PATH="$HOME/.local/bin:$PATH"
  codex login --device-auth
  codex login status
  # Put your Telegram settings in .env; leave Windows-specific CODEX_COMMAND blank.
  .venv/bin/python -B bot.py --test-telegram
  .venv/bin/python -B bot.py --preview-ai
  # Follow deploy/ORACLE.md to stop PC monitoring and transfer a fresh state snapshot.
  bash deploy/install-service.sh
NEXT
