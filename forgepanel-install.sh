#!/usr/bin/env bash
#
# ForgePanel-Dashboard installer / uninstaller
# Detects distro, installs python3 (with sqlite3), git, curl and the optional
# python3 cryptography package, clones/updates the repo, prepares database/,
# opens the firewall port and runs server.py as a systemd/OpenRC service.
# After starting it checks GET /api/health to confirm the service is up.
#
# Usage:
#   bash forgepanel-install.sh            # install
#   bash forgepanel-install.sh uninstall  # remove service, optionally database/, dir, packages
#

set -euo pipefail

START_TIME=$(date +%s)
REPO_URL="https://github.com/JeffTheTroubleshooter/ForgePanel-Dashboard.git"
DEFAULT_PORT=8084
DEFAULT_DIR="/opt/forgepanel"
SERVICE_NAME="forgepanel"

# ── helpers ────────────────────────────────────────────────────────────────
die() { echo -e "\n\033[1;31mError:\033[0m $*" >&2; exit 1; }
info() { echo -e "\033[1;34m==>\033[0m $*"; }
ok() { echo -e "\033[1;32m==>\033[0m $*"; }
warn() { echo -e "\033[1;33m==>\033[0m $*"; }
prompt() { local var="$1" msg="$2" default="$3"; read -rp "$msg [$default]: " input; printf -v "$var" '%s' "${input:-$default}"; }
ask_yn() { local msg="$1" default="${2:-n}"; local ans; read -rp "$msg [y/N]: " ans; ans="${ans:-$default}"; [[ "$ans" =~ ^[Yy] ]]; }

elapsed() {
  local end=$(date +%s)
  local secs=$((end - START_TIME))
  printf '%dm %ds' $((secs/60)) $((secs%60))
}

# ── distro detection ───────────────────────────────────────────────────────
detect_distro() {
  if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    ID="${ID:-}"
    ID_LIKE="${ID_LIKE:-}"
  else
    die "Cannot detect distro (/etc/os-release missing)."
  fi

  case "$ID" in
    debian|ubuntu|linuxmint|pop|elementary|zorin) PKG="apt" ;;
    fedora|rhel|rocky|almalinux|centos|ol)         PKG="dnf" ;;
    arch|manjaro|endeavouros|garuda)               PKG="pacman" ;;
    opensuse*|sles)                                PKG="zypper" ;;
    alpine)                                        PKG="apk" ;;
    *)
      case "$ID_LIKE" in
        *debian*|*ubuntu*) PKG="apt" ;;
        *rhel*|*fedora*)   PKG="dnf" ;;
        *arch*)            PKG="pacman" ;;
        *suse*)            PKG="zypper" ;;
        *) die "Unsupported distro: $ID (ID_LIKE=$ID_LIKE). Supported: Debian/Ubuntu, Fedora/RHEL, Arch, openSUSE, Alpine." ;;
      esac
      ;;
  esac
  info "Detected: $ID ($PKG)"
}

# ── packages ───────────────────────────────────────────────────────────────
pkg_install() {
  # install packages; never abort the whole installer on one failure
  case "$PKG" in
    apt)    sudo apt-get update -qq || true
            sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
    dnf)    sudo dnf install -y "$@" ;;
    pacman) sudo pacman -Sy --needed --noconfirm "$@" ;;
    zypper) sudo zypper --non-interactive install "$@" ;;
    apk)    sudo apk add --no-cache "$@" ;;
  esac
}

install_requirements() {
  info "Installing required packages..."
  # python3 incl. the sqlite3 module, git, curl
  case "$PKG" in
    apt)    pkg_install python3 git curl ca-certificates libsqlite3-0 openssl ;;
    dnf)    pkg_install python3 python3-libs git curl sqlite-libs openssl ;;
    pacman) pkg_install python git curl sqlite openssl ;;
    zypper) pkg_install python3 git curl libsqlite3-0 openssl ;;
    apk)    pkg_install python3 git curl sqlite-libs openssl ;;
  esac || die "Could not install required packages."

  info "Installing optional python3 cryptography (encryption at rest)..."
  case "$PKG" in
    apt|dnf|zypper) pkg_install python3-cryptography ;;
    pacman)         pkg_install python-cryptography ;;
    apk)            pkg_install py3-cryptography ;;
  esac || warn "python3 cryptography package could not be installed."

  command -v python3 >/dev/null 2>&1 || die "python3 not found after install."
  python3 -c "import sqlite3" 2>/dev/null || die "python3 cannot import sqlite3. Install your distro's python3 sqlite module and re-run."
  ok "python3 $(python3 -c 'import sys; print(sys.version.split()[0])') with sqlite3 OK"
  if python3 -c "import cryptography" 2>/dev/null; then
    ok "python3 cryptography OK: dashboard data and scan results will be encrypted at rest"
  else
    warn "python3 cryptography is MISSING: data in database/forgepanel.db will be stored UNENCRYPTED."
    warn "Install it later (python3-cryptography / python-cryptography / py3-cryptography) and restart the service."
  fi
}

# ── prompts ────────────────────────────────────────────────────────────────
ask_user() {
  echo
  prompt INSTALL_DIR "Install directory" "$DEFAULT_DIR"
  prompt PORT "Port to listen on" "$DEFAULT_PORT"

  HTTPS_FLAG=""
  if ask_yn "Serve over HTTPS with a self-signed certificate (database/certs/)?"; then
    HTTPS_FLAG=" --https"
  fi

  PROXY_FLAGS=""
  if ask_yn "Is ForgePanel behind a reverse proxy (Nginx Proxy Manager, Caddy, Traefik)?"; then
    prompt PROXY_CIDR "Trusted proxy IP or CIDR (only these may set X-Forwarded-For)" "172.16.0.0/12"
    python3 -c 'import ipaddress,sys; ipaddress.ip_network(sys.argv[1], strict=False)' "$PROXY_CIDR" 2>/dev/null \
      || die "Proxy must be a valid IP or CIDR, e.g. 172.16.0.0/12 or 192.168.1.10."
    PROXY_FLAGS=" --trusted-proxy $PROXY_CIDR"
  fi

  [[ "$PORT" =~ ^[0-9]+$ ]] || die "Port must be a number."
  (( PORT >= 1 && PORT <= 65535 )) || die "Port out of range."
}

# ── clone / update ─────────────────────────────────────────────────────────
setup_files() {
  info "Setting up files in $INSTALL_DIR ..."
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Existing install found — pulling latest..."
    sudo git -C "$INSTALL_DIR" pull --ff-only || die "git pull failed. Local edits in $INSTALL_DIR? Back up database/ and resolve, then re-run."
  else
    sudo mkdir -p "$(dirname "$INSTALL_DIR")"
    sudo git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  fi
  [[ -f "$INSTALL_DIR/server.py" ]] || die "server.py missing in $INSTALL_DIR."
  sudo chown -R "$USER:$(id -gn)" "$INSTALL_DIR" 2>/dev/null || true

  # database folder: only the service user may read it
  sudo mkdir -p "$INSTALL_DIR/database"
  sudo chown "$USER:$(id -gn)" "$INSTALL_DIR/database"
  sudo chmod 700 "$INSTALL_DIR/database"
  [[ -f "$INSTALL_DIR/database/forgepanel.db" ]] && sudo chmod 600 "$INSTALL_DIR/database/forgepanel.db"
  ok "Files ready (database: $INSTALL_DIR/database, mode 0700)."
}

# ── firewall ───────────────────────────────────────────────────────────────
open_firewall() {
  if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
    sudo ufw allow "$PORT/tcp" >/dev/null && ok "ufw: opened $PORT/tcp"
  elif command -v firewall-cmd >/dev/null 2>&1 && sudo firewall-cmd --state >/dev/null 2>&1; then
    sudo firewall-cmd --permanent --add-port="$PORT/tcp" >/dev/null && sudo firewall-cmd --reload >/dev/null \
      && ok "firewalld: opened $PORT/tcp"
  else
    info "No active ufw/firewalld found; firewall unchanged."
  fi
}

# ── service ────────────────────────────────────────────────────────────────
stop_old_static_server() {
  # installs before server.py ran "python3 -m http.server"; free the port
  pkill -f "python3? -m http.server $PORT" 2>/dev/null || true
  pkill -f "python3? -m http.server.*--directory $INSTALL_DIR" 2>/dev/null || true
}

create_service() {
  local python
  python=$(command -v python3)
  stop_old_static_server

  if command -v systemctl >/dev/null 2>&1; then
    info "Creating systemd service..."
    sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<UNIT
[Unit]
Description=ForgePanel Dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$python $INSTALL_DIR/server.py --port $PORT --host 0.0.0.0$HTTPS_FLAG$PROXY_FLAGS
Restart=on-failure
RestartSec=3
User=$USER
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
    sudo systemctl restart "$SERVICE_NAME"   # restart so upgrades from http.server switch over
    ok "systemd service enabled and (re)started."

  elif command -v rc-service >/dev/null 2>&1; then
    info "Creating OpenRC service (Alpine)..."
    sudo tee "/etc/init.d/${SERVICE_NAME}" >/dev/null <<OPENRC
#!/sbin/openrc-run
description="ForgePanel Dashboard"
command="$python"
command_args="$INSTALL_DIR/server.py --port $PORT --host 0.0.0.0$HTTPS_FLAG$PROXY_FLAGS"
command_user="$USER"
directory="$INSTALL_DIR"
umask=077
command_background=true
pidfile="/run/${SERVICE_NAME}.pid"
OPENRC
    sudo chmod +x "/etc/init.d/${SERVICE_NAME}"
    sudo rc-update add "$SERVICE_NAME" default
    sudo rc-service "$SERVICE_NAME" restart
    ok "OpenRC service enabled and started."
  else
    warn "No systemd or OpenRC detected. Starting in background..."
    pkill -f "$INSTALL_DIR/server.py" 2>/dev/null || true
    (cd "$INSTALL_DIR" && umask 077 && nohup "$python" server.py --port "$PORT" --host 0.0.0.0 $HTTPS_FLAG $PROXY_FLAGS >>"$INSTALL_DIR/database/server.log" 2>&1 &)
    ok "Started in background (log: $INSTALL_DIR/database/server.log)."
  fi
}

# ── health check ───────────────────────────────────────────────────────────
check_health() {
  local scheme="http" opts=(-fsS)
  if [[ -n "$HTTPS_FLAG" ]]; then
    scheme="https"
    opts+=(-k)
  fi
  local url="$scheme://127.0.0.1:$PORT/api/health" body=""
  info "Waiting for the service to answer $url ..."
  for _ in $(seq 1 20); do
    if body=$(curl "${opts[@]}" --max-time 3 "$url" 2>/dev/null) && [[ "$body" == *'"app": "forgepanel"'* ]]; then
      ok "Service is up: $body"
      return 0
    fi
    sleep 1
  done
  warn "No ForgePanel answer from $url after 20 s. Check the service:"
  warn "  sudo systemctl status $SERVICE_NAME   (or: sudo rc-service $SERVICE_NAME status)"
  warn "  log: $INSTALL_DIR/database/server.log"
  return 1
}

# ── uninstall ──────────────────────────────────────────────────────────────
remove_service() {
  info "Removing service..."

  if command -v systemctl >/dev/null 2>&1; then
    if systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1; then
      sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
      sudo systemctl disable "$SERVICE_NAME" 2>/dev/null || true
      sudo rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
      sudo systemctl daemon-reload
      ok "systemd service removed."
    else
      warn "No systemd service found."
    fi
  elif command -v rc-service >/dev/null 2>&1; then
    if [[ -f "/etc/init.d/${SERVICE_NAME}" ]]; then
      sudo rc-service "$SERVICE_NAME" stop 2>/dev/null || true
      sudo rc-update del "$SERVICE_NAME" default 2>/dev/null || true
      sudo rm -f "/etc/init.d/${SERVICE_NAME}"
      ok "OpenRC service removed."
    else
      warn "No OpenRC service found."
    fi
  fi
  # best-effort stop of background processes (server.py or an old http.server)
  pkill -f "forgepanel.*/server.py|[/ ]server.py --port" 2>/dev/null || true
  pkill -f "python3 -m http.server" 2>/dev/null || true
}

backup_database() {
  local dir="$1" dest
  dest="$HOME/forgepanel-database-backup-$(date +%Y%m%d-%H%M%S)"
  sudo cp -a "$dir/database" "$dest" && sudo chown -R "$USER:$(id -gn)" "$dest" 2>/dev/null
  ok "Database backed up to $dest"
}

remove_files() {
  local dir="${1:-$DEFAULT_DIR}"
  if [[ ! -d "$dir" ]]; then
    warn "Install directory $dir not found."
    return
  fi
  local has_db=0 delete_db=0
  [[ -f "$dir/database/forgepanel.db" ]] && has_db=1
  if (( has_db )); then
    if ask_yn "Delete the database ($dir/database: account, services, scan results)?"; then
      delete_db=1
    fi
  fi
  if ask_yn "Remove install directory $dir?"; then
    (( has_db && ! delete_db )) && backup_database "$dir"
    sudo rm -rf "$dir"
    ok "Directory removed."
  else
    if (( delete_db )); then
      sudo find "$dir/database" -mindepth 1 ! -name README.md ! -name .gitkeep -delete
      ok "Database deleted."
    fi
    info "Kept $dir"
  fi
}

remove_packages() {
  if ask_yn "Also remove packages (python3 and git)? (may affect other software)"; then
    info "Removing packages..."
    case "$PKG" in
      apt)    sudo apt-get remove -y python3 git || true ;;
      dnf)    sudo dnf remove -y python3 git || true ;;
      pacman) sudo pacman -Rns --noconfirm python git || true ;;
      zypper) sudo zypper --non-interactive remove python3 git || true ;;
      apk)    sudo apk del python3 git || true ;;
    esac
    ok "Package removal finished."
  else
    info "Packages kept."
  fi
}

uninstall() {
  echo "ForgePanel-Dashboard uninstaller"
  echo "================================="
  detect_distro
  remove_service

  echo
  prompt INSTALL_DIR "Install directory to remove" "$DEFAULT_DIR"
  remove_files "$INSTALL_DIR"
  remove_packages

  echo
  ok "Uninstall finished in $(elapsed)"
}

# ── main ───────────────────────────────────────────────────────────────────
main() {
  local mode="${1:-install}"

  case "$mode" in
    uninstall|remove|--uninstall|-u)
      uninstall
      ;;
    install|"")
      echo "ForgePanel-Dashboard installer"
      echo "==============================="
      detect_distro
      install_requirements
      ask_user
      setup_files
      create_service
      open_firewall
      check_health || true

      local ip
      ip=$(hostname -I 2>/dev/null | awk '{print $1}') || ip="localhost"

      echo
      ok "Done in $(elapsed)"
      echo
      local scheme="http"
      [[ -n "$HTTPS_FLAG" ]] && scheme="https"
      echo "  Open:  $scheme://${ip:-localhost}:$PORT"
      [[ -n "$HTTPS_FLAG" ]] && echo "  HTTPS: self-signed cert in $INSTALL_DIR/database/certs/ (see README: trusting the certificate)"
      echo "  Files: $INSTALL_DIR"
      echo "  Database: $INSTALL_DIR/database/forgepanel.db (back this folder up)"
      echo "  Health: $scheme://${ip:-localhost}:$PORT/api/health"
      echo
      echo "Default login (change on first use): admin / forgepanel"
      echo
      echo "Service commands:"
      if command -v systemctl >/dev/null 2>&1; then
        echo "  sudo systemctl status $SERVICE_NAME"
        echo "  sudo systemctl restart $SERVICE_NAME"
      elif command -v rc-service >/dev/null 2>&1; then
        echo "  sudo rc-service $SERVICE_NAME status"
        echo "  sudo rc-service $SERVICE_NAME restart"
      fi
      echo
      echo "To uninstall later: bash $0 uninstall"
      ;;
    *)
      die "Unknown option: $mode. Use: $0 [install|uninstall]"
      ;;
  esac
}

main "$@"
