# How often is a mined event right?

Precision of the events whyfence mines from commit messages, on a labelled sample from
three public repositories. It measures **precision only**: how many of the events it
reports are what they claim to be, not how many it misses.

| kind | click | flask | requests | **all** |
|---|---|---|---|---|
| incident | 7/10 | 4/10 | 6/10 | **17/30 = 57%** |
| decision | 6/9 | 5/10 | 6/10 | **17/29 = 59%** |
| revert | 7/9 | 5/10 | 8/10 | **20/29 = 69%** |
| rule (constraint) | 4/4 | 4/10 | 2/5 | **10/19 = 53%** |
| all | 24/32 | 18/40 | 22/35 | **64/107 = 60%** |

Measured with whyfence 0.4.0 (development). The most common false positive, in every
kind, is a change to docs, examples, tests or CI: "Fix broken example", "Update
testing.rst", `Revert "Add python 3.9 to CI."`. Every rejected label says why in
[`labels.jsonl`](labels.jsonl).

## What was measured

| repository | commit | date |
|---|---|---|
| pallets/flask | `49943b5a94413a530761350f04e896f76e8d620e` | 2026-10-30 |
| psf/requests | `3c7baaa05ab4aa49cc819ce3a819d260de19c157` | 2026-10-26 |
| pallets/click | `e38188e9e205005d5af5f9e59df51904c984586a` | 2026-11-12 |

Each repository was indexed with `whyfence ingest REPO --db X.db --no-transcripts`
(commit messages only; no agent sessions). [`sample.py`](sample.py) then drew up to ten
commit-mined events of each kind per repository, with a fixed seed per (repository,
kind), so the same index always gives the same sample. There are fewer than ten when a
repository has fewer (click has 9 decisions and 9 reverts; requests has 5 rules, click 4).

## What counts as correct

The criteria were written before labelling:

- **incident**: the commit fixes a defect in the code's behaviour: a bug, crash,
  regression, wrong result or leak. Not docs, examples, tests, CI, typing, refactors or
  new features.
- **decision**: the summary states a choice about the code *and* the reason for it.
- **revert**: the commit undoes an earlier change to the code (not to docs, examples,
  tests, CI or assets).
- **rule**: the summary states a lasting rule for how the code must or must not be used
  or changed, true beyond this one commit.

When a message didn't say what it changed ("fix for #160", "fix merge"), the label
followed the files the commit touched.

## Who labelled, and the limits

The labels were made by Claude, the AI assistant that also wrote the classifier, as a
single labeller with no second opinion. That is a conflict of interest worth knowing,
which is why every label is in [`labels.jsonl`](labels.jsonl), each rejection has a
reason, and the sample can be rebuilt exactly. Disagreements are welcome as issues or
pull requests against that file.

The sample is small (107 events), from three Python web libraries; other projects,
languages and commit styles will differ. Recall is not measured. Agent-session events
(Claude Code, Gemini, Codex) are not covered: they come from private transcripts.

## Reproduce

```console
$ git clone https://github.com/pallets/flask && git -C flask checkout 49943b5a
$ whyfence ingest flask --db flask.db --no-transcripts      # likewise requests, click
$ uv run python eval/precision.py --check flask=flask.db requests=requests.db click=click.db
```

`--check` confirms the indexes still yield the labelled sample. If the classifier has
changed, it reports how many events are new or gone: the labels then describe the older
classifier and need relabelling.
