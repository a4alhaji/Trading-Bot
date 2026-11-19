#!/bin/zsh
# Control the trading bot background service.
#   ./botctl.sh install   - start the service now and at every login (auto-restarts on crash)
#   ./botctl.sh uninstall - stop it and stop auto-starting
#   ./botctl.sh restart   - restart it (e.g. after editing config.py)
#   ./botctl.sh status    - is it running?
#   ./botctl.sh logs      - follow the bot log
LABEL=com.tradingbot.bot
PLIST=~/Library/LaunchAgents/$LABEL.plist
DOMAIN=gui/$(id -u)
DIR=${0:A:h}

case "$1" in
  install)
    if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null && ! launchctl print $DOMAIN/$LABEL >/dev/null 2>&1; then
      echo "The bot is already running outside the service. Stop it first (Ctrl+C in its terminal)."; exit 1
    fi
    launchctl bootstrap $DOMAIN $PLIST && echo "Installed. Dashboard: http://localhost:8765" ;;
  uninstall) launchctl bootout $DOMAIN/$LABEL && echo "Stopped and removed from login startup." ;;
  restart)   launchctl kickstart -k $DOMAIN/$LABEL && echo "Restarted." ;;
  status)
    if launchctl print $DOMAIN/$LABEL >/dev/null 2>&1; then
      launchctl print $DOMAIN/$LABEL | grep -E "state =|pid =|last exit code" | sed 's/^[[:space:]]*//'
    else echo "Service not installed."; fi ;;
  logs)      tail -f "$DIR/bot.log" ;;
  *)         echo "Usage: $0 {install|uninstall|restart|status|logs}"; exit 1 ;;
esac
