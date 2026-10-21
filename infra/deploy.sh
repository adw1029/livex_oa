#!/usr/bin/env bash
# Build the Lambda bundle, deploy the stack, upload the UI to S3.
# Usage: make deploy   (uses AWS_PROFILE=adw, AWS_REGION=us-east-1 unless set)
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_PROFILE="${AWS_PROFILE:-adw}" AWS_REGION="${AWS_REGION:-us-east-1}"
STACK=livex-assistant
PYTHON="${PYTHON:-.venv/bin/python}"

# Linux arm64 wheels for the Lambda runtime, so this works from a Mac without Docker.
rm -rf .build && mkdir -p .build/function
"$PYTHON" -m pip install -q -r requirements.txt -t .build/function \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.13 --only-binary=:all:
cp -R app calcom_mcp infra/run.sh .build/function/
find .build/function -name __pycache__ -prune -exec rm -rf {} +

sam deploy --template-file infra/template.yaml --stack-name "$STACK" --resolve-s3 \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset

out() { aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
aws s3 sync app/static "s3://$(out BucketName)" --delete
aws cloudfront create-invalidation --distribution-id "$(out DistributionId)" --paths '/*' --query Invalidation.Id --output text
echo "Live at: $(out Url)"
