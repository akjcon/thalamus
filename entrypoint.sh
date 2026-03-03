#!/bin/bash
# On first deploy (or if volume is empty), seed the world model
# from the baked-in copy. After that, the volume persists across deploys.

if [ -d "/data" ]; then
    # Railway volume is mounted at /data
    # Symlink memory -> /data/memory so all code paths work unchanged
    if [ ! -d "/data/memory" ]; then
        echo "First deploy: copying seed data to volume..."
        cp -r /app/memory /data/memory
    fi
    rm -rf /app/memory
    ln -sf /data/memory /app/memory
    echo "Using persistent volume at /data/memory"
else
    echo "No volume mounted — using local memory/ (data will not persist across deploys)"
fi

exec "$@"
