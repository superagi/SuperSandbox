#!/bin/bash
set -e

# Start virtual framebuffer
Xvfb :1 -screen 0 1280x720x24 &
sleep 1

# Start window manager
fluxbox &
sleep 1

# Start VNC server (no password for dev)
x11vnc -display :1 -forever -nopw -rfbport 5900 -shared &
sleep 1

# Start noVNC web client
websockify --web /usr/share/novnc 6080 localhost:5900 &

echo "Desktop ready — noVNC at port 6080"

# Keep alive
wait
