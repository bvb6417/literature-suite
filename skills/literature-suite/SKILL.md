---
name: literature-suite
description: Search journal literature, expand from seed papers, complete metadata, download legal full-text PDFs and export RIS for Zotero with the local literature_suite.py CLI. Use when the user asks to find papers on a topic, look up a DOI, fill in missing citation fields, build a reference list, or download PDFs for a list of DOIs.
---

# Literature Suite CLI

Set `SUITE` to the installed entry file, for example `D:/tools/literature-suite/literature_suite.py`, and call it with `python`. Every command prints one JSON object to stdout. Exit code 0 means everything succeeded; any other code means at least one item failed, so read `ok` and the per-item results.

## Commands

| Task | Command |
| --- | --- |
| Keyword search | `python SUITE keyword "<query>" --limit 20` |
| Title search | `python SUITE search "<title>" --mode title` |
| Exact DOI lookup | `python SUITE doi <DOI>` |
| One-hop expansion from seeds | `python SUITE expand <DOI or title> [...] --direction both\|references\|citations --limit 30` |
| Complete metadata | `python SUITE enrich <DOI> [...] -o refs.ris` or `--input records.json` |
| Download full text | `python SUITE download <DOI> [...] --json-lines` |
| Check configured channels | `python SUITE check` |

- `-o` accepts `.json`, `.csv` or `.ris`; stdout always carries the JSON result.
- Search records use the fields `doi, title, authors, year, journal, volume, issue, pages, article_number, date, journal_abbreviation, issn, language, abstract, publisher`.
- `enrich` never overwrites a field that already has a value. A record listed as `缺失部分` usually means the sources do not publish that field (Elsevier journals often have no issue number), not that the lookup failed.
- `download --sources` takes `openalex,elsevier,webvpn`; `webvpn` means whichever institutional access the user selected in the GUI.
- Long download batches: prefer `--json-lines` and report progress from the `start` / `result` / `complete` events.

## Workflow

1. Search or expand, then show the user a short table (year, title, journal, DOI) and let them pick.
2. Run `enrich` on the chosen DOIs and save a `.ris` file the user can import into Zotero via File → Import.
3. Download only the DOIs the user asked for. Report the source and page count for each success and the `reason` for each failure.

## Boundaries

- Only open-access copies, official publisher APIs and the user's own institutional subscription are used. Never suggest Sci-Hub, mirrors or shared accounts.
- If a result asks for institutional login or human verification, stop and ask the user to finish it in the GUI (全局配置 → 刷新机构登录). Never enter credentials.
- Keep batches modest; do not loop retries against publisher servers.
