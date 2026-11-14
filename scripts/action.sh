#!/usr/bin/env bash
# The whyfence GitHub Action (see action.yml). Every input arrives as an environment
# variable, never pasted into this script, so branch names and titles can't inject code.
set -euo pipefail

if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  echo "::error::whyfence needs the full history. Check out with actions/checkout and 'fetch-depth: 0'."
  exit 1
fi

# Which whyfence to run.
version="${INPUT_VERSION:-}"
if [ -z "$version" ] && [[ "${ACTION_REF:-}" =~ ^v([0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
  version="${BASH_REMATCH[1]}"
fi
spec="${WHYFENCE_FROM:-whyfence${version:+==$version}}"  # WHYFENCE_FROM: a local checkout, for testing
wf() { uvx --quiet --python 3.12 --from "$spec" whyfence "$@"; }

# What the change is based on. GitHub checks out a merge commit whose first parent is the
# base branch as it is now, which can be newer than the base recorded in the event.
base="${INPUT_BASE:-}"
if [ -z "$base" ]; then
  if [ -n "${PR_HEAD_SHA:-}" ] && [ "$(git rev-parse -q --verify HEAD^2 || true)" = "$PR_HEAD_SHA" ]; then
    base="$(git rev-parse HEAD^1)"
  elif [ -n "${PR_BASE_SHA:-}" ]; then
    base="$PR_BASE_SHA"
  else
    echo "::error::Not a pull request, so there's no base to compare with. Set the action's 'base' input."
    exit 1
  fi
fi

db="${RUNNER_TEMP:-/tmp}/whyfence-index.db"
out="${RUNNER_TEMP:-/tmp}/whyfence"
mkdir -p "$out"

# CI has no agent sessions: commit messages and the team's .whyfence/events/ are the sources.
wf ingest . --db "$db" --no-transcripts
wf check "$base" --db "$db" --format json >"$out/check.json"
wf check "$base" --db "$db" --format markdown >"$out/comment.md"

fences="$(jq '.fences | length' "$out/check.json")"
echo "fences=$fences" >>"$GITHUB_OUTPUT"
cat "$out/comment.md" >>"$GITHUB_STEP_SUMMARY"
echo "whyfence: $fences changed function(s) or class(es) with recorded history."

# One comment per pull request, updated on every push. If a later push stops touching
# fenced code, the old comment is updated to say so rather than left behind.
if [ "${INPUT_COMMENT:-true}" = "true" ] && [ -n "${PR_NUMBER:-}" ]; then
  marker="$(head -n 1 "$out/comment.md")"
  existing="$(gh api --paginate "repos/$GH_REPO/issues/$PR_NUMBER/comments" \
    --jq ".[] | select(.body | startswith(\"$marker\")) | .id" 2>/dev/null | head -n 1 || true)"
  if [ -n "$existing" ]; then
    gh api --method PATCH "repos/$GH_REPO/issues/comments/$existing" -F "body=@$out/comment.md" >/dev/null \
      || echo "::warning::Couldn't update the whyfence comment; the result is in the job summary."
  elif [ "$fences" -gt 0 ]; then
    gh api --method POST "repos/$GH_REPO/issues/$PR_NUMBER/comments" -F "body=@$out/comment.md" >/dev/null \
      || echo "::warning::Couldn't comment (pull requests from forks get a read-only token); the result is in the job summary."
  fi
fi

if [ -n "${INPUT_FAIL_ON:-}" ]; then
  # shellcheck disable=SC2086  # one argument per kind
  wf check "$base" --db "$db" --fail-on $INPUT_FAIL_ON >/dev/null || {
    echo "::error::This change touches code with a recorded event of kind: $INPUT_FAIL_ON. See the job summary."
    exit 1
  }
fi
