#!/bin/bash
set -euo pipefail
echo "Scaffolding Zero Trust Advisor Agent environment..."
BLUEPRINT="${1:-rag-hybrid}"
ENV="${2:-dev}"
REGION="${3:-us-east-1}"
echo "Blueprint: $BLUEPRINT"
echo "Environment: $ENV"
echo "Region: $REGION"
mkdir -p "live/$ENV/$REGION/$BLUEPRINT"
echo "Scaffolded live/$ENV/$REGION/$BLUEPRINT"
