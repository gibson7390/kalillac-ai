# Decision 12: finalizing Word documents without the update prompt

Prototype evaluation, 2026-11-16. Development only. **Nothing here is in the product:** generated Word
files still mark the contents field dirty, so Microsoft Word refreshes it (after its prompt) and the
page numbers it shows are exact. That stays until the owner accepts a finalization design.

## The problem, as verified

* The prompt ("this document contains fields that may refer to other files") comes from the contents
  field's `w:dirty` flag. Verified in Word 16: the same file without the flag opens with no prompt.
* Word desktop computes `PAGE` and `SECTIONPAGES` in headers and footers itself. Viewers that do not
  show the cached result, which is `1`. **LibreOffice is one of them: it renders the benchmark report's
  footer as "Page 2 of 1"**, the owner's observation.
* No engine available on the server paginates like Word except, as measured below, LibreOffice. The
  PDF engine (Typst) put 3 of the benchmark's 18 headings one page early.

## Prototype

LibreOffice 26.8.3 (TDF's Linux build, SHA-256 verified, unpacked in a throwaway WSL folder without
root; two NSS libraries fetched with `apt-get download`; its own user profile; nothing installed
system-wide). It is used only as a **pagination measurer**: it renders the generated `.docx` to PDF, and
that PDF is read for the body's page count and each heading's page. Those numbers are written into the
generated document's own XML: the `SECTIONPAGES` cached result and the contents field's cached entries.
Then the dirty flag is cleared. LibreOffice's own output is discarded, so the document's design,
styles, embedded fonts and fields are exactly the generator's. Script:
`scratchpad/pagination/finalize_prototype.py` (development scratch, not committed).

## Results

| Check | Result |
|---|---|
| Heading pages, LibreOffice vs Word (report, research, proposal) | **16 of 16 equal**; page counts equal |
| Finalized contents vs Word's own recomputation (`TablesOfContents.Update()`) | **Equal, 11 of 11 entries** (report and research) |
| Cold open in Word with alerts on | **No prompt** for either finalized file |
| Contents still a real, refreshable field | Yes: Word updated it normally |
| LibreOffice view of the finalized report | "Page 1 of 3", "Page 2 of 3", "Page 3 of 3"; contents with page numbers |
| Unpacked size | 738 MB (plus 1.6 MB of NSS libraries) |
| Peak memory per document | 226 to 281 MB |
| Time per document (this machine, WSL) | 19 to 56 s cold, 16 to 18 s warm |

## Risks and limits

* **Attack surface.** LibreOffice is a large C++ code base with a long history of document-parsing
  vulnerabilities. The input here is the generator's own output (its structure is ours, its text is the
  customer's); it must never see customer uploads in this role. Owner instruction: it does not go into
  the existing file worker.
* **Isolation it would need:** its own process sandbox (separate user, no network, read-only image,
  private temporary directory, memory and CPU limits, seccomp), one document per process, killed on a
  time limit, and a fresh profile per run.
* **Latency.** 16 to 56 s per document is too slow to finalize inside a chat request's file step today
  (the worker's default limit is 20 s); it would run after delivery or the file would wait for it.
* **Agreement with Word is measured, not guaranteed.** Three documents agree exactly. Not yet tested:
  Arabic and other right-to-left documents, long documents, tables and images split across pages,
  Word for Mac and Word on the web. With finalization, a cached number that disagreed with Word would be
  what Word shows until the reader updates the field; with the current prompt Word always shows its
  own exact numbers. That is why adoption waits for agreement on every benchmark document.
* **Presentation of the cached contents.** The prototype writes plain "title, tab, page" lines; a
  product version must write Word's own contents paragraphs (TOC 1 and TOC 2 styles, dot leaders,
  hyperlinks to the headings) so the cold-open view looks exactly like Word's refreshed one.

## Recommendation

Adopt only as a separate, sandboxed finalization service, after the remaining tests above pass, and only
when the finalized numbers agree with Word on every benchmark document. Until then keep the accurate
Word refresh (the current behaviour). The owner accepts the final customer experience.
