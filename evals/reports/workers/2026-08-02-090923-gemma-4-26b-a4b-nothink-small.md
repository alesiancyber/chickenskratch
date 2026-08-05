# Worker eval — gemma-4-26b-a4b-nothink (adapt=small) — 2026-08-02-090923

**82/111 cases passed all 3 runs** — 249/333 runs clean — 0 retries — 12.4 min total

Slowest (mean s/run): record-long-transcript-full-pipeline (12.1s), record-long-braindump-nothing-dropped (11.9s), long-meeting-notes-nothing-vanishes (9.4s)

| case | clean runs | retries | mean s/run | first failure |
|---|---|---|---|---|
| answer-how-many-names-the-items | 0/3 | 0 | 2.5s | only 0 quote(s) survive verification (need 1); offered: {'Projects/Detection Backlog.md': '- [ ] kerberoasting detection |
| answer-exact-figure-right-note | 0/3 | 0 | 1.8s | only 0 quote(s) survive verification (need 1); offered: {'Finance/Ingest Costs.md': "Last month's ingest overage came to |
| answer-refuses-when-notes-are-silent | 3/3 | 0 | 0.9s |  |
| answer-synthesis-cites-both-notes | 3/3 | 0 | 2.5s |  |
| answer-ignores-the-near-miss-note | 3/3 | 0 | 1.3s |  |
| answer-list-question-lists-all | 0/3 | 0 | 2.9s | only 0 quote(s) survive verification (need 1); offered: {'Meetings/2026-07-25 Incident Retro.md': 'Finding one: no docum |
| answer-quote-hostile-characters | 3/3 | 0 | 1.4s |  |
| answer-long-pack-needle | 0/3 | 0 | 2.6s | only 0 quote(s) survive verification (need 1); offered: {'Ops/Journal 2026-06.md': "the actual root cause was the expire |
| record-bare-task-list-autofiles | 3/3 | 0 | 4.3s |  |
| record-meeting-detected-and-filed | 0/3 | 0 | 3.8s | gate: only 56% of 'Draft the remediation plan for conditional access gaps' traces to any source line |
| record-status-note-stays-capture | 3/3 | 0 | 3.1s |  |
| record-transcript-becomes-minutes | 0/3 | 0 | 7.9s | gate: invented word(s) ['prepare'] in 'Prepare the list of rules to be archived' |
| record-owner-prefix-lands-in-owed-by | 1/3 | 0 | 4.0s | kind='capture', expected 'meeting' |
| record-waiting-on-split | 2/3 | 0 | 4.0s | kind='meeting', expected 'capture' |
| record-week-anchors-survive | 3/3 | 0 | 4.2s |  |
| record-decisions-are-not-tasks | 0/3 | 0 | 3.1s | gate: invented word(s) ['outcome'] in 'framework debate outcome' |
| record-no-tasks-invented | 3/3 | 0 | 2.1s |  |
| record-hostile-markdown-verbatim-survives | 3/3 | 0 | 2.4s |  |
| record-long-braindump-nothing-dropped | 0/3 | 0 | 11.9s | gate: invented word(s) ['daily'] in 'daily brain dump' |
| record-long-transcript-full-pipeline | 0/3 | 0 | 12.1s | '(?i)escalation runbook': due=None, expected '{plus7}' |
| urgent-priority-mapping | 3/3 | 0 | 1.7s |  |
| someday-item-kept | 3/3 | 0 | 1.2s |  |
| relative-date-two-weeks | 3/3 | 0 | 2.1s |  |
| owed-commitment-followup | 3/3 | 0 | 2.0s |  |
| completed-item-excluded | 3/3 | 0 | 1.5s |  |
| no-tasks-pure-status | 3/3 | 0 | 0.8s |  |
| decision-is-not-a-task | 3/3 | 0 | 0.8s |  |
| multiple-tasks-mixed | 3/3 | 0 | 2.6s |  |
| date-not-repeated-in-text | 3/3 | 0 | 1.4s |  |
| cancelled-plan-is-not-a-task | 3/3 | 0 | 0.8s |  |
| negated-instruction-not-inverted | 3/3 | 0 | 1.8s |  |
| two-people-owe-things | 3/3 | 0 | 2.6s |  |
| delegated-by-me-is-owed | 3/3 | 0 | 2.4s |  |
| waiting-on-phrasing | 3/3 | 0 | 1.8s |  |
| critical-severity-mapping | 3/3 | 0 | 1.4s |  |
| low-priority-phrasing | 3/3 | 0 | 1.4s |  |
| numbers-are-not-dates | 0/3 | 0 | 1.9s | expected exactly 1 task(s), got 2; extra: ['Put them in lockbox 7'] |
| in-a-week-relative-date | 3/3 | 0 | 1.5s |  |
| eod-tomorrow | 3/3 | 0 | 1.5s |  |
| duplicate-mention-extracted-once | 3/3 | 0 | 1.4s |  |
| reference-material-is-not-a-task | 3/3 | 0 | 0.7s |  |
| task-buried-mid-paragraph | 3/3 | 0 | 1.4s |  |
| braindump-fragments | 3/3 | 0 | 2.2s |  |
| forwarded-email-style | 3/3 | 0 | 1.4s |  |
| past-recap-plus-single-action | 3/3 | 0 | 1.4s |  |
| suggestion-we-should-is-a-task | 3/3 | 0 | 1.4s |  |
| date-and-priority-both-stated | 3/3 | 0 | 1.6s |  |
| negation-hold-off-variant | 0/3 | 0 | 2.2s | '(?i)sign.?off': due=None, expected '{tomorrow}' |
| cancellation-scrapped-variant | 3/3 | 0 | 1.4s |  |
| already-handled-variant | 3/3 | 0 | 1.4s |  |
| promised-phrasing-variant | 0/3 | 0 | 1.9s | no returned task matches '(?i)summari' |
| going-to-send-variant | 0/3 | 0 | 1.4s | no returned task matches '(?i)tracker\|load' |
| drop-everything-priority-variant | 3/3 | 0 | 1.6s |  |
| weekly-wrap-no-tasks-variant | 3/3 | 0 | 0.9s |  |
| signoff-not-a-task-variant | 3/3 | 0 | 0.8s |  |
| ought-to-someday-variant | 3/3 | 0 | 1.4s |  |
| in-five-days-variant | 3/3 | 0 | 1.6s |  |
| buried-task-variant | 3/3 | 0 | 1.5s |  |
| dedupe-reworded-variant | 3/3 | 0 | 1.3s |  |
| extract-week-plans-vs-deadlines | 3/3 | 0 | 2.7s |  |
| blocker-splits-into-task-and-owed | 3/3 | 0 | 2.0s |  |
| long-meeting-notes-nothing-vanishes | 0/3 | 0 | 9.4s | '(?i)debug.level\|staging cluster': due=None, expected '{today}' |
| long-email-thread-quoted-history-ignored | 0/3 | 0 | 3.4s | '(?i)questionnaire': owed_by="vendor's CISO", expected None |
| long-noise-paste-single-task | 3/3 | 0 | 1.7s |  |
| grouping-thematic-split | 3/3 | 0 | 1.6s |  |
| grouping-customer-boundary | 3/3 | 0 | 1.7s |  |
| grouping-three-way-split | 3/3 | 0 | 3.3s |  |
| grouping-misfit-stays-out | 3/3 | 0 | 2.0s |  |
| grouping-same-verb-different-systems | 3/3 | 0 | 1.7s |  |
| grouping-three-client-boundary | 3/3 | 0 | 2.3s |  |
| grouping-personal-vs-work | 3/3 | 0 | 1.8s |  |
| grouping-research-and-build-same-topic | 3/3 | 0 | 2.4s |  |
| grouping-prod-incident-vs-feature-work | 3/3 | 0 | 1.7s |  |
| grouping-two-misfits-dont-force-merge | 3/3 | 0 | 2.0s |  |
| grouping-ten-tasks-three-themes | 3/3 | 0 | 3.9s |  |
| grouping-vendor-vs-internal-same-product | 0/3 | 0 | 1.8s | ['t-ven000000001', 't-ven000000002'] should share a group; got ['Microsoft Licensing', 'Microsoft Sentinel Support'] |
| grouping-client-possessive-variant | 3/3 | 0 | 1.9s |  |
| grouping-client-ticket-prefix-variant | 3/3 | 0 | 2.0s |  |
| grouping-misfit-different-domain-variant | 3/3 | 0 | 1.7s |  |
| overlap-plain-duplicate | 3/3 | 0 | 1.3s |  |
| overlap-related-not-duplicate | 3/3 | 0 | 1.0s |  |
| overlap-client-boundary | 3/3 | 0 | 1.4s |  |
| overlap-mixed-batch | 3/3 | 0 | 1.6s |  |
| overlap-keep-prefers-the-dated-line | 3/3 | 0 | 1.6s |  |
| overlap-rephrased-duplicate | 3/3 | 0 | 1.5s |  |
| overlap-dependency-is-related | 3/3 | 0 | 1.1s |  |
| overlap-same-noun-different-work | 0/3 | 0 | 1.2s | ['t-verb00000001', 't-verb00000002']: verdict='related', expected 'distinct' |
| overlap-system-boundary | 0/3 | 0 | 1.3s | ['t-syst00000001', 't-syst00000002']: verdict='related', expected 'distinct' |
| overlap-five-pair-batch | 0/3 | 0 | 4.8s | ['t-btch00000003', 't-btch00000004']: verdict='related', expected 'distinct' |
| plan-picks-nothing-from-a-quiet-pool | 3/3 | 0 | 0.7s |  |
| plan-restraint-when-day-is-full | 3/3 | 0 | 1.2s |  |
| plan-prefers-unblocking-people | 3/3 | 0 | 1.2s |  |
| plan-hears-time-pressure-in-the-wording | 3/3 | 0 | 0.9s |  |
| plan-skips-blocked-work | 3/3 | 0 | 1.2s |  |
| plan-oldest-external-item-surfaces | 3/3 | 0 | 1.1s |  |
| plan-id-discipline-large-pool | 3/3 | 0 | 1.9s |  |
| schema-meeting-missing-id | 3/3 | 0 | 1.7s |  |
| schema-meeting-date-from-filename | 3/3 | 0 | 2.4s |  |
| schema-meeting-prose-untouched | 3/3 | 0 | 1.7s |  |
| schema-research-missing-sources | 0/3 | 0 | 1.8s | plan not simulable — cannot vouch for its effect |
| schema-research-id-and-sources | 0/3 | 0 | 2.6s | plan not simulable — cannot vouch for its effect |
| schema-research-missing-status | 0/3 | 0 | 1.9s | fix leaves the note nonconforming — would re-draft every pass |
| schema-tasks-stamp-two-in-order | 0/3 | 0 | 2.4s | plan not simulable — cannot vouch for its effect |
| schema-tasks-stamp-preserves-metadata | 0/3 | 0 | 2.2s | plan not simulable — cannot vouch for its effect |
| schema-typo-is-not-ours-to-fix | 0/3 | 0 | 2.1s | plan not simulable — cannot vouch for its effect |
| schema-done-task-stamped-without-reopening | 0/3 | 0 | 1.8s | plan not simulable — cannot vouch for its effect |
| schema-meeting-with-action-items | 3/3 | 0 | 4.7s |  |
| schema-unlisted-issues-left-alone | 3/3 | 0 | 1.9s |  |
| schema-typo-in-prose-variant | 3/3 | 0 | 2.2s |  |
| schema-typo-in-task-with-metadata-variant | 0/3 | 0 | 2.1s | plan not simulable — cannot vouch for its effect |
| schema-empty-tags-not-populated-variant | 3/3 | 0 | 1.7s |  |
