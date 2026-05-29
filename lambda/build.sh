#!/usr/bin/env bash
# Assemble the Lambda deployment package under lambda/build/.
# Terraform's archive_file data source zips that directory into lambda.zip.
#
# Run this before `terraform apply` whenever handler.py, the finder/reporter
# scripts, or requirements.txt change.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BUILD="$HERE/build"

rm -rf "$BUILD"
mkdir -p "$BUILD"

# Source files: handler + the two CLI modules it imports.
cp "$HERE/handler.py" "$BUILD/"
cp "$REPO/orphaned_spark_instances.py" "$BUILD/"
cp "$REPO/generate_outputs.py" "$BUILD/"

# Pip-install deps into the package root. --platform/--only-binary keeps wheels
# compatible with the Lambda Python 3.12 runtime (x86_64).
python3 -m pip install \
  --target "$BUILD" \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --upgrade \
  -r "$HERE/requirements.txt"

echo "built lambda package at $BUILD"
