---
name: research
description: Research a technical topic properly using the Microsoft Learn docs tools - search for orientation, then fetch the full authoritative page(s) for depth, then synthesize with source links and optionally save to the vault. Use when the user asks for architecture guidance, best practices, how-tos, or documentation on Microsoft/Azure topics.
---

# Research: search for orientation, fetch for depth

Search results are excerpt chunks (≤500 tokens each) — good for finding the
right pages, too shallow to answer from alone. Depth lives in the full page.

## Procedure

1. **Search**: ms_learn__microsoft_docs_search with a specific query.
2. **Pick the authoritative page(s)**: look at the result URLs — when several
   chunks come from the same URL, that page is the canonical source. Choose
   one or two, at most.
3. **Fetch them**: ms_learn__microsoft_docs_fetch on each chosen URL for the
   complete content. Never skip this step — do not answer from chunks alone.
4. **Synthesize**: answer from the fetched content, structured to the user's
   question. Every claim traces to the docs; if the docs don't cover
   something, say so rather than filling from memory.
5. **Cite**: end with a Sources list of the URLs you fetched.

## Saving to the vault

If the user asked to save (or accepts your offer): create
`Research/<Topic>.md` containing the synthesis, the Sources list, and a
one-line frontmatter `status: reference`. Offer this; don't do it
unrequested.

## For code questions

Use ms_learn__microsoft_code_sample_search in addition to docs search when
the user needs implementation examples; the same fetch-for-depth rule
applies to the pages the samples come from.
