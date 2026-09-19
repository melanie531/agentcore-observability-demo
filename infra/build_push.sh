#!/bin/bash
# Build the agent container (linux/arm64, required by AgentCore Runtime) and push to ECR.
# Usage: AWS_PROFILE=platform-dev-takeover ./infra/build_push.sh <ecr_uri> [tag]
set -euo pipefail

ECR_URI="${1:?usage: build_push.sh <ecr_uri> [tag]}"
TAG="${2:-latest}"
REGION="us-west-2"
REGISTRY="${ECR_URI%%/*}"

cd "$(dirname "$0")/../agent"

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

docker buildx build --platform linux/arm64 -t "$ECR_URI:$TAG" --push .

echo "pushed $ECR_URI:$TAG"
