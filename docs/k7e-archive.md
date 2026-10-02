# Exploratory archive ingestion

Use a separate `K7E_HOME` for exploratory material when an operational seat
must consume only its operating knowledge.

```bash
K7E_HOME=/path/to/archive k7e distill --archive transcript.txt
K7E_HOME=/path/to/archive k7e distill --archive messages.json --dry-run
K7E_HOME=/path/to/archive k7e recall "What ideas remain undecided?"
K7E_HOME=/path/to/archive k7e recall "What changed?" --include-superseded
```

`--archive` accepts UTF-8 text, Markdown, or a JSON object with `records`:

```json
{
  "records": [
    {
      "source_id": "synthetic-message-01",
      "text": "Synthetic example: perhaps a garden could help; no decision yet.",
      "stated_at": "2026-01-01T12:00:00Z"
    }
  ]
}
```

`text` is a required string. `source_id` is optional: missing identifiers use an
automatically generated snapshot-hash/record-index locator. Original identifiers
are retained when supplied; a generated locator does not claim knowledge of
an upstream original author or message. `stated_at` and `effective_at`
are optional strings supplied by the importer, retained verbatim. Supply the
original message or transcript identifier, rather than an extraction-generated
identifier. Plain text uses its snapshot hash as its source identifier. Input
JSON is an explicit archive format; arbitrary harness JSON requires conversion.
Audio transcription and speaker identification belong upstream.

`origin` is an optional descriptive string and defaults to `unknown`.
`derived_from` is an optional list of source locators, such as previous memory
IDs, original message identifiers, or versioned file/span paths. An autonomous
agent can supply these from its inputs and cited evidence; human tagging is
not required. They are retained with each source reference through append,
reindex and recall. These edges are recorded claims of derivation, not verified
identity or authority; K7E does not automatically resolve or authorize them.
An unknown upstream origin still has a traceable capture path and byte hash.

Archive ingestion retains undecided ideas and planning intentions without the
operational extractor's three-item limit. Its `kind` is `idea`, `decision`,
`instruction`, or `observation`. Archive content is prefixed with its supplied source identifier so even a model
returning an imperative retains explicit attribution. Instruction records describe source claims;
they grant no authority. Typed records use `Source Claims`, rather than
`Verified Protocol`. `active` means unretired, not verified or currently
effective. Kind is an extraction hypothesis that may need human correction.

The store retains the original bytes as `sources/<sha256>.txt`, including
sources from which extraction yields no candidates. Each candidate must carry
a nonempty verbatim supporting quote. A quote absent from its input chunk is
rejected and recorded as a distillation failure. `source_refs` in frontmatter
contains a versioned JSON array with source ID, source hash, snapshot path and
half-open `start`/`end` character offsets. For JSON input, offsets refer to the
**decoded record text** selected by `record_index`; for text input, they refer
to the decoded file. Offsets count Python Unicode characters, not bytes or
UTF-16 units. Hashes cover the entire original file bytes.

Snapshots are source versions, independent of embeddings and index rebuilds.
New versions retain old snapshots. Bytes are retained before UTF-8 decoding or
JSON validation. Invalid input also writes `sources/<sha256>.error.json` with
its source identifier and validation error, and the CLI reports failure. Exact duplicate typed content of the same
kind merges source references. Append accumulates references and does not
increment the typed record's operational `verification_count`. The original ingestion hash remains stable after append and supersession, so
replaying the initial claim returns its existing node without reactivating it.
Similar titles do
not merge archive records: fuzzy deduplication and consolidation apply only
to operational records. This deliberately retains more near duplicates. Operational `compile` excludes
typed records, preventing an exploratory claim from becoming an authoritative
compiled reference. A tag needs at least three untyped operational entries
to compile; archive synthesis remains a separate future feature.

Recall returns provenance, kind, status and supersession metadata with each
source entry, passes them to synthesis, and prints original source locators.
Recall supplies `[SRC-N]` source handles scoped to the returned evidence and
asks the model to cite those alongside entry handles `[K7E-BBB-NNNNN]`. Code
resolves accepted handles to original identifiers, retained snapshot hashes and
Unicode spans. Unknown or malformed answer handles are flagged in additive
`citation_errors`; invalid stored references appear in `source_ref_errors` and
receive no handle. `citations` lists available validated source handles and
`resolved_citations` lists accepted handles actually used by the answer.
The API remains `(answer, entries)`, and the CLI prints warnings to stderr and
only resolved original sources in its validated-source footer. An answer with
warnings is not rejected or silently rewritten. Callers must inspect errors.

The validator checks snapshot namespace/confinement, SHA-256, record index,
structured original-ID/date agreement and half-open Unicode ranges. Unknown
origin is valid. It reads at most 8 MiB of source bytes and considers at most
64 references per recall; exceeding those validation budgets is explicitly
flagged. Excerpts are capped at 256 characters. These reader limits do not
bound snapshot retention or total answer context. Raw `source_refs` remains
available for audit even when invalid. Legacy entries lacking retained snapshots
have entry citations, not validated original-source handles. This establishes
reference integrity, not semantic entailment, factual truth, upstream identity
or complete claim coverage. Inspect snapshots for disputed claims. Confidence, append counts and vector similarity are
ranking signals, not proof or authorization.

Archive ingestion does not infer correction relationships from similarity,
import order or model scores. Use `k7e supersede <old> <new>` for explicit
corrections; the old entry and its evidence remain accessible. Default search
and recall exclude retired entries; naming an exact retired node ID still requires the historical flag.
`get` provides a direct audit read. Importers should provide
stated/effective dates when known. Dates alone do not retire records.

`--dry-run` creates no snapshots or nodes. `--archive` does not accept `--job`:
recoverable operational capture jobs keep their existing contract. Archive
replays deduplicate exact typed content; model variation may produce siblings.
The default distill extractor and capture correction pass retain their
operational behavior. Archive mode does not run capture-specific corrections.

## Evaluation and limits

`tests/test_archive.py` exercises synthetic source fidelity, version retention,
append/reindex/recall, typed distinctions, explicit supersession, dry runs,
invalid spans, and retrieval plumbing with an unhelpful title. The semantic
counterexample uses a supplied candidate; it proves track integration, not
embedding-model quality. Existing retrieval evaluation remains the lexical
baseline. No public benchmark or real voice-memo longitudinal evaluation is
implied by these tests.

Raw snapshots are preserved but **not indexed for search**. Archive retrieval
still searches extracted nodes. Missing extraction therefore remains a recall
risk even when the original source is retained. Source-first and combined
retrieval are future evaluation conditions. Archive media extraction, automatic
ordinary-transcript corrections, source deletion/retention policy, and
conflict adjudication remain separate work.

The embedding track retains its existing linear scan and configured model.
It drops out when the embeddings table has more than 10,000 rows, including
retired/orphan rows counted before the status-filtered scan. Lexical search
continues. Reindex resets usage ranking signals; reindex with embeddings
requests regeneration. This is not an archive-scale retrieval guarantee.

## Relationship to operational provenance

`source` remains the latest writing turn and `sources` remains files that turn
read, as defined in [distillation](k7e-distillation.md). Existing operational
producers/readers keep that contract. `source_refs` is an additive structured
extension for versioned original capture spans and derivation links; it does
not reinterpret the writing turn as the upstream author or override the file
list. A non-capture archive input can have unknown origin and an automatic
locator without a claimed writing turn. Append keeps the latest operational
stamp and accumulated structured references, so their different timestamps
answer different questions. The Markdown node remains canonical; citation
handles and validation errors are transient recall output. No metadata grants
authority. Operational packing still needs an evaluated extension to retain
these richer references and their byte cost; raw snapshot placement/growth and
default-writer omission remain separate gaps.

Records without explicit source IDs receive the deterministic locator `sha256:<snapshot-hash>#record-<index>`. Recall verifies this locator against retained bytes and record position; Path-derived locators for ID-less JSON records are unverified audit metadata. This locator identifies a retained record, not an authenticated author.

A record is typed when its frontmatter `kind` line holds any nonblank text, read as written: `kind: []` and `kind: 0` are typed. `get --json` reports that test as `kind`, and r4t's knowledge pack keeps only entries it reports as `null`. Default search, recall, list, statistics and embedding backfill exclude typed archive records. Use `search --include-archive` or `recall --include-archive` for exploratory retrieval. Original source IDs and raw snapshot excerpts are retained in audit metadata, but synthesis receives only source handles and descriptive notes; CLI citation footers print snapshot and span locators.

Validated references require a retained quote hash matching the referenced span. References without that hash remain audit metadata and receive no validated source handle. Quote binding and snapshot integrity do not prove semantic entailment.

Replaying a retired record with the same snapshot hash, record index and Unicode span returns its retired node without appending or creating an active replacement, even when descriptive wording, path spelling or the extracted kind changes. An exact duplicate of an active record on that span returns the active record first, so two records of different kinds from one span stay separate, and retiring one leaves the other active. A different claim that quotes exactly a retired span also returns the retired record; the span is the identity, and its retirement covers every reading of it. Changed export bytes or shifted spans can create a separate active archive record; retirement is not inferred across those cases. All such records remain excluded from default operational readers. Review and explicitly supersede duplicates across exports; ingestion grants no authority.

Archive inputs are limited to 8 MiB per file and rejected before snapshot creation when oversized. Directory ingestion ignores unrelated valid JSON and media; explicitly supplied unsupported inputs fail. A directory file that is oversized, is torn JSON, or holds a `records` value that is not an array is skipped and named on stderr with its reason; the other files are taken, and the run exits 1 so a script sees the skip. `check` reports invalid structured references and snapshot hash/path errors without repairing or deleting evidence. Offline auditing checks every reference and snapshot, using a fresh cache per reference and an 8 MiB bound per file; recall alone has a total per-query budget. This bounds individual reads, not total audit time or retained storage.

Typed titles are attributed with `The source reported:` in storage and exploratory retrieval, including synthesis. This describes a source claim rather than an operational heading.
