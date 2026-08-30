#!/usr/bin/env bash
#
# Scaffold a new llm-wiki project: raw/, wiki/, wiki/index.md, wiki/log.md,
# and a schema file (AGENTS.md or CLAUDE.md) seeded from the skill's template.
#
# Usage:
#   init.sh --name "Wiki Name" --domain "One-line domain description" \
#            [--dir path/to/project] [--schema-file AGENTS.md|CLAUDE.md]
#
# Safe to re-run: never overwrites existing files, only creates what's missing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"

NAME="My Wiki"
DOMAIN="Description of what this wiki tracks"
TARGET_DIR="."
SCHEMA_FILE="AGENTS.md"

usage() {
  cat <<'EOF'
Usage: init.sh --name "Wiki Name" --domain "One-line domain description" [options]

Options:
  --name STRING          Human-readable wiki name (default: "My Wiki")
  --domain STRING        One-line description of what the wiki tracks
  --dir PATH             Target project directory (default: current directory)
  --schema-file NAME     AGENTS.md or CLAUDE.md (default: AGENTS.md)
  -h, --help             Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    --dir) TARGET_DIR="$2"; shift 2 ;;
    --schema-file) SCHEMA_FILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ "$SCHEMA_FILE" != "AGENTS.md" && "$SCHEMA_FILE" != "CLAUDE.md" ]]; then
  echo "Warning: --schema-file is usually AGENTS.md or CLAUDE.md (got: $SCHEMA_FILE)" >&2
fi

mkdir -p "$TARGET_DIR"
cd "$TARGET_DIR"

created=()
skipped=()

make_dir() {
  local d="$1"
  if [[ -d "$d" ]]; then
    skipped+=("$d/")
  else
    mkdir -p "$d"
    created+=("$d/")
  fi
}

write_file_if_missing() {
  local path="$1"
  local content="$2"
  if [[ -e "$path" ]]; then
    skipped+=("$path")
  else
    printf '%s\n' "$content" > "$path"
    created+=("$path")
  fi
}

make_dir "raw"
make_dir "wiki"
make_dir "wiki/sources"
make_dir "wiki/entities"
make_dir "wiki/concepts"
make_dir "wiki/synthesis"

TODAY="$(date +%Y-%m-%d)"

INDEX_CONTENT="# Index

Catalog of all pages in this wiki. Updated on every ingest.

## Sources

<!-- - [Title](sources/YYYY-MM-DD - Title.md) — one-line summary. (YYYY-MM-DD) -->

## Entities

<!-- - [Name](entities/Name.md) — one-line summary. -->

## Concepts

<!-- - [Concept](concepts/Concept.md) — one-line summary. -->

## Synthesis / filed answers

<!-- - [Title](synthesis/Title.md) — one-line summary. (YYYY-MM-DD) -->
"

LOG_CONTENT="# Log

Append-only. Do not edit past entries except to fix typos.

## [$TODAY] init | Wiki created
- Created raw/, wiki/, wiki/index.md, wiki/log.md, $SCHEMA_FILE
"

write_file_if_missing "wiki/index.md" "$INDEX_CONTENT"
write_file_if_missing "wiki/log.md" "$LOG_CONTENT"

if [[ -e "$SCHEMA_FILE" ]]; then
  skipped+=("$SCHEMA_FILE")
else
  {
    echo "# $NAME — Schema"
    echo
    echo "Domain: $DOMAIN"
    echo
    # Append the template body, stripping the surrounding "# Schema template"
    # heading and the fenced code block markers from schema-template.md.
    awk '
      /^```markdown$/ { inblock=1; next }
      /^```$/ { if (inblock) { inblock=0; next } }
      inblock { print }
    ' "$SKILL_DIR/references/schema-template.md" | tail -n +5
  } > "$SCHEMA_FILE"
  created+=("$SCHEMA_FILE")
fi

echo "llm-wiki init complete in: $(pwd)"
echo
if [[ ${#created[@]} -gt 0 ]]; then
  echo "Created:"
  printf '  %s\n' "${created[@]}"
fi
if [[ ${#skipped[@]} -gt 0 ]]; then
  echo "Already existed (left untouched):"
  printf '  %s\n' "${skipped[@]}"
fi
echo
echo "Next: open $SCHEMA_FILE and fill in the {{placeholders}} based on a short"
echo "conversation with the user (page types, naming, ingest style, output formats)."
