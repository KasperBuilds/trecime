#!/bin/bash

# Inject the Camoufox session storage state from the Railway environment variable
mkdir -p ~/.camofox/profiles/585dd6332347ac93c97e0e379a14f8c0
echo "$CAMOFOX_STORAGE_STATE" > ~/.camofox/profiles/585dd6332347ac93c97e0e379a14f8c0/storage-state.json

# Start the Camofox-Browser Server in the background
cd camofox-browser
unset CAMOUFOX_EXECUTABLE
npm start &
cd ..

# Wait a few seconds for the server to be ready
echo "Waiting for Camofox server to start..."
sleep 10

# Run both monitors
echo "Starting monitors..."
export CAMOFOX_URL="http://localhost:${PORT:-9377}"

# Auronzo monitor in background
python3 auronzo_monitor.py &

# Real Madrid monitor in foreground (keeps container alive)
python3 rm_monitor.py
