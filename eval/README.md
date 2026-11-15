# How often is a mined event right?

Precision of the events whyfence mines from commit messages. It measures **precision
only**: how many of the events it reports are what they claim to be, not how many it
misses. Precision is reported **per kind only**: every sample is stratified, so the kinds
are not in the proportions whyfence produces them, and pooling them would weight them
wrongly.

## whyfence 0.4.1: the fresh blind sample (the current numbers)

80 commit-mined events, 20 per kind, drawn by [`sample_fresh.py`](sample_fresh.py)
**after** the 0.4.1 changes were made, from events no one had labelled before
([`fresh-80-blind.csv`](fresh-80-blind.csv)). Two labellers labelled it independently:
Claude (the AI assistant that wrote the classifier; labels sealed with SHA-256
`2faabdeb…dcdd71` before the reviewer's file existed) and a reviewer (`2d040f73…57968b7`).

| kind | repositories behind the rows | Claude | reviewer | agreement | Cohen's κ |
|---|---|---|---|---|---|
| incident | flask 5, requests 5, click 5, gin 5 | 10/20 = 50% | 9/20 = 45% | 19/20 | 0.90 |
| decision | requests 8, flask 7, gin 5 | 14/20 = 70% | 13/20 = 65% | 19/20 | 0.89 |
| revert | requests 13, gin 7 | 18/20 = 90% | 17/20 = 85% | 19/20 | 0.77 |
| rule: **django (19 of 20 rows)** | django 19, gin 1 | 10/20 = 50% | 16/20 = 80% | 14/20 | 0.40 |

Read each row only for the repositories named in it. The rule row is essentially a django
figure: flask, requests, click and gin have few commit-mined rules, and earlier samples
used them up. Click had no unlabelled decisions or reverts left, and flask no unlabelled
reverts.

**Rules are where the labellers disagree** (κ 0.40, six of the nine disagreements). The
reviewer accepts a lasting behaviour written as a change ("Made introspection always
return a unicode …", "It will now always return whether the file exists"); Claude rejects
those as change descriptions. Until that criterion is settled, rule precision on this
sample is best read as somewhere between 50% and 80%. The other kinds agree closely.

Both label files are here ([`fresh-80-claude.csv`](fresh-80-claude.csv),
[`fresh-80-reviewer.csv`](fresh-80-reviewer.csv)), and every disagreement, with both
reasons, is in [`fresh-80-disagreements.csv`](fresh-80-disagreements.csv);
[`compare.py`](compare.py) recomputes the table. Eight of the nine are rows the reviewer
marked borderline.

## whyfence 0.4.0: the first sample

On a labelled sample from three public repositories (Claude's labels only):

| kind | click | flask | requests | **all three** |
|---|---|---|---|---|
| incident | 7/10 | 4/10 | 6/10 | **17/30 = 57%** |
| decision | 6/9 | 5/10 | 6/10 | **17/29 = 59%** |
| revert | 7/9 | 5/10 | 8/10 | **20/29 = 69%** |
| rule (constraint) | 4/4 | 4/10 | 2/5 | **10/19 = 53%** |

Up to ten events of each kind per repository. Measured with whyfence 0.4.0 (development). The most common false positive, in every
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

Added after the second labeller's review, and applied **from the next sample on** (the
samples above were not relabelled):

- a **decision** about tests, CI or docs only does not count;
- a **revert** of a revert of a merge does not count (it is housekeeping and says nothing
  about the code).

## A second labeller

An independent reviewer labelled an 80-row blind sample without seeing Claude's labels:
the 50-row file plus 15 more rules and 15 more incidents (the rules from flask, requests,
click and, because those three have only 20 commit-mined rules between them, 14 from
django). Claude labelled the 30 new rows before reading the reviewer's file.
[`blind-80-claude.csv`](blind-80-claude.csv) and
[`blind-80-reviewer.csv`](blind-80-reviewer.csv) hold both sets, with a reason for each
of the reviewer's labels.

| kind | rows | Claude | reviewer | agreement | Cohen's κ |
|---|---|---|---|---|---|
| incident | 27 | 15/27 = 56% | 14/27 = 52% | 26/27 | 0.93 |
| decision | 14 | 11/14 = 79% | 9/14 = 64% | 12/14 | 0.66 |
| revert | 15 | 9/15 = 60% | 8/15 = 53% | 12/15 | 0.59 |
| rule | 24 | 14/24 = 58% | 15/24 = 62% | 21/24 | 0.74 |

The two agree on 71 of 80 rows. **All 9 disagreements are rows the reviewer marked
borderline**; each is in [`blind-80-disagreements.csv`](blind-80-disagreements.csv) with
both reasons. The reviewer was stricter on test-only decisions and on reverts of reverted
merges; Claude was stricter on quoted error messages and explanations.

**Known labelling error, #30** (flask `128bc76af0f6`, `Revert "make
external_url_handler example py3 compliant"`): Claude labelled it incorrect, as reverting
a docs example, going by the commit message. The reviewer checked the diff, which changes
a `raise` statement in `flask/helpers.py`: it reverts code, so it is correct. Claude's
label is left as it was given, so the comparison shows what each labeller actually did.

These 80 rows are now a **development set**: the 0.4.1 filters were designed from their
rejections, so numbers on them are not an unbiased measure.

### 0.4.1 on the development set (not an unbiased measure)

[`devset.py`](devset.py) re-mines the indexes the 80 rows came from and keeps a row only
if the same event, with the same text, is still mined. One row (#63) changed text: its
label no longer applies, so it counts as neither kept nor dropped.

| kind | labeller | before (0.4.0) | after (0.4.1) | rows dropped | dropped but labelled correct |
|---|---|---|---|---|---|
| incident | Claude | 15/27 = 56% | 15/16 = 94% | 11 | 0 |
| incident | reviewer | 14/27 = 52% | 14/16 = 88% | 11 | 0 |
| decision | Claude | 11/14 = 79% | 10/10 = 100% | 4 | 1 |
| decision | reviewer | 9/14 = 64% | 9/10 = 90% | 4 | 0 |
| revert | Claude | 9/15 = 60% | 7/8 = 88% | 7 | 2 |
| revert | reviewer | 8/15 = 53% | 8/8 = 100% | 7 | 0 |
| rule | Claude | 14/24 = 58% | 13/16 = 81% | 7 | 1 |
| rule | reviewer | 15/24 = 62% | 12/16 = 75% | 7 | 2 |

What 0.4.1 loses here: #59 (a usage rule both labellers accepted, stated in a docs-only
commit), #54 (a rule the reviewer accepted, from a commit that only removed a test), and,
by Claude's original labels, the two reverts of reverted merges and one test-only decision
that the new definitions no longer count. The unbiased number comes from the fresh blind
sample (`fresh-80-blind.csv`, drawn by [`sample_fresh.py`](sample_fresh.py) after the
changes, labelled separately by both labellers).

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
