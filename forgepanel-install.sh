#!/usr/bin/env bash
#
# ForgePanel-Dashboard installer / uninstaller
# Detects distro, ensures python3 + git are present, clones the repo,
# and sets up a simple service to serve it.
#
# Usage:
#   bash forgepanel-install.sh            # install
#   bash forgepanel-install.sh uninstall  # remove service (+ optional packages)
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

# ── package check + install ────────────────────────────────────────────────
ensure_package() {
  local cmd="$1"
  local pkg="$2"

  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd already installed"
    return 0
  fi

  info "$cmd not found — installing $pkg..."
  case "$PKG" in
    apt)
      sudo apt-get update -qq
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$pkg"
      ;;
    dnf)
      sudo dnf install -y "$pkg"
      ;;
    pacman)
      sudo pacman -Sy --noconfirm "$pkg"
      ;;
    zypper)
      sudo zypper --non-interactive install "$pkg"
      ;;
    apk)
      sudo apk add --no-cache "$pkg"
      ;;
  esac
  ok "$pkg installed"
}

install_requirements() {
  info "Checking required packages..."
  ensure_package python3 python3
  ensure_package git git
}

# ── prompts ────────────────────────────────────────────────────────────────
ask_user() {
  echo
  prompt INSTALL_DIR "Install directory" "$DEFAULT_DIR"
  prompt PORT "Port to listen on" "$DEFAULT_PORT"

  [[ "$PORT" =~ ^[0-9]+$ ]] || die "Port must be a number."
  (( PORT >= 1 && PORT <= 65535 )) || die "Port out of range."
}

# ── clone / update ─────────────────────────────────────────────────────────
setup_files() {
  info "Setting up files in $INSTALL_DIR ..."
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Existing install found — pulling latest..."
    sudo git -C "$INSTALL_DIR" pull --ff-only
  else
    sudo mkdir -p "$(dirname "$INSTALL_DIR")"
    sudo git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  fi
  sudo chown -R "$USER:$USER" "$INSTALL_DIR" 2>/dev/null || true
  ok "Files ready."
}

# ── service ────────────────────────────────────────────────────────────────
create_service() {
  local python
  python=$(command -v python3)

  if command -v systemctl >/dev/null 2>&1; then
    info "Creating systemd service..."
    sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<UNIT
[Unit]
Description=ForgePanel Dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$python -m http.server $PORT
Restart=on-failure
RestartSec=3
User=$USER

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable --now "$SERVICE_NAME"
    ok "systemd service enabled and started."

  elif command -v rc-service >/dev/null 2>&1; then
    info "Creating OpenRC service (Alpine)..."
    sudo tee "/etc/init.d/${SERVICE_NAME}" >/dev/null <<OPENRC
#!/sbin/openrc-run
description="ForgePanel Dashboard"
command="$python"
command_args="-m http.server $PORT"
directory="$INSTALL_DIR"
command_background=true
pidfile="/run/${SERVICE_NAME}.pid"
OPENRC
    sudo chmod +x "/etc/init.d/${SERVICE_NAME}"
    sudo rc-update add "$SERVICE_NAME" default
    sudo rc-service "$SERVICE_NAME" restart
    ok "OpenRC service enabled and started."
  else
    warn "No systemd or OpenRC detected. Starting in background..."
    nohup "$python" -m http.server "$PORT" --directory "$INSTALL_DIR" >/dev/null 2>&1 &
    ok "Started in background (PID $!)."
  fi
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
  else
    # best-effort kill of background python http.server
    pkill -f "python3 -m http.server" 2>/dev/null || true
    warn "No service manager found — attempted to stop background process."
  fi
}

remove_files() {
  local dir="${1:-$DEFAULT_DIR}"
  if [[ -d "$dir" ]]; then
    if ask_yn "Remove install directory $dir?"; then
      sudo rm -rf "$dir"
      ok "Directory removed."
    else
      info "Kept $dir"
    fi
  else
    warn "Install directory $dir not found."
  fi
}

remove_packages() {
  if ask_yn "Also remove packages (python3 and git)? (may affect other software)"; then
    info "Removing packages..."
    case "$PKG" in
      apt)
        sudo apt-get remove -y python3 git || true
        ;;
      dnf)
        sudo dnf remove -y python3 git || true
        ;;
      pacman)
        sudo pacman -Rns --noconfirm python git || true
        ;;
      zypper)
        sudo zypper --non-interactive remove python3 git || true
        ;;
      apk)
        sudo apk del python3 git || true
        ;;
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

      local ip
      ip=$(hostname -I 2>/dev/null | awk '{print $1}') || ip="localhost"

      echo
      ok "Done in $(elapsed)"
      echo
      echo "  Open:  http://${ip:-localhost}:$PORT"
      echo "  Files: $INSTALL_DIR"
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
