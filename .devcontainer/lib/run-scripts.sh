#!/usr/bin/env bash
set -euo pipefail

# Runs every *.sh in a directory in sorted order. *.skip.sh files are ignored,
# giving developers an opt-out without deleting shared scripts.
run_script_directory() {
  local script_directory="$1"

  if [ ! -d "$script_directory" ]; then
    echo "No script directory found: $script_directory"
    return 0
  fi

  local found_script="false"
  while IFS= read -r -d '' script_path; do
    case "$script_path" in
      *.skip.sh)
        echo "Skipping disabled script: $script_path"
        continue
        ;;
    esac

    found_script="true"
    echo "Running: $script_path"
    bash "$script_path"
  done < <(find "$script_directory" -maxdepth 1 -type f -name '*.sh' -print0 | sort -z)

  if [ "$found_script" = "false" ]; then
    echo "No scripts to run in: $script_directory"
  fi
}
