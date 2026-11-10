#!/bin/bash
# All state (database, wiki, kill switch) lives on the Railway volume when one is mounted at /data.
if [ -d "/data" ]; then
    export THAL_DATA_DIR=/data/thal
    mkdir -p "$THAL_DATA_DIR"
    echo "State on persistent volume at $THAL_DATA_DIR"
else
    echo "No volume mounted: state stays in the container and is lost on redeploy"
fi
exec "$@"
