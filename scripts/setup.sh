#!/bin/bash
set -euo pipefail
echo "Setting up Zero Trust Advisor Agent..."
pip install -e ".[dev]"
echo "Setup complete!"
