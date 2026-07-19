---
name: research
description: Find the authoritative Microsoft Learn documentation URLs for a topic and present them as a ranked link list - no summarizing. Use when the user asks for docs, architecture guidance, best practices, or how-tos on Microsoft/Azure topics.
---

# Research: deliver links, not summaries

The user reads documentation themselves and does not want your paraphrase.
Your job is to find the right pages fast and hand over the URLs.

## Procedure

1. Search ms_learn__microsoft_docs_search with a specific query. If the
   topic has clearly distinct aspects (e.g. architecture vs. security),
   run at most one additional search variant.
2. Group results by page: strip the #fragment from each URL and treat all
   results sharing the base URL as ONE page. More results from one page
   means it's more central.
3. Reply with a ranked link list — **each page appears exactly once**,
   most central first:

   1. **Page title** — https://learn.microsoft.com/... (base URL, no fragment)
      one factual line on what the page covers (drawn from the result, not
      your interpretation); if several sections of it matched, list them:
      *sections: considerations, recommendations, scenario details*
      — flag the top entry as "start here"

## Rules

- URLs are copied **verbatim** from tool results. Never construct,
  complete, shorten, or guess a URL.
- No summaries, no "key takeaways", no synthesis. Links + one factual
  line each.
- Code questions: also run ms_learn__microsoft_code_sample_search and
  include those page URLs in the list.
- If the user asks to save: create `Research/<Topic> links.md` with the
  list (frontmatter `status: reference`).
- Fetch a full page (ms_learn__microsoft_docs_fetch) ONLY if the user
  explicitly asks for content, a summary, or an answer from the docs.
