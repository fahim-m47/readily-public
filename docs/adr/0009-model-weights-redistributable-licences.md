# Model weights may ship under any licence Readily can hand a reader

Status: accepted (2026-09-01), amended (2026-09-03) · Supersedes [ADR 0008](0008-model-weights-licence-policy.md) §1 · Keeps ADR 0008 §2 and §3 · Builds on [ADR 0003](0003-baked-in-hash-pinned-catalog.md)

ADR 0008 closed a real hole: weights are not linked into a process, so the
dependency licence gates never look at them. Its stated reason was right. The Catalog is
the app handing a reader weights, and a licence that attaches a use policy to
those weights attaches it to the reader. Its rule was a proxy for that reason,
"permissive only", and the proxy over-rejects. It refuses CC-BY-4.0, which
asks Readily for attribution and asks the reader for nothing, and it refuses
every community-licensed model (Llama, OpenRAIL-M).

The intention is not "open weights only". Every open model that may be
downloaded to a reader's machine for local narration is a
candidate, and Readily carries whatever obligations the licence puts on the
party doing the handing. This ADR replaces the proxy with the reason.

## The decisions

1. **A Catalog entry's weights must be under a licence that permits Readily
   to redistribute them to an end user for that user's ordinary personal
   use.** Two things fail that bar. A licence that narrows the reader's own
   use below "personal, on your own machine" (non-commercial and research-only
   scopes: CC-BY-NC-4.0, the Mistral Research License), because Readily cannot
   promise a reader the narration is theirs to use. And a distribution Readily
   cannot fetch anonymously (an HF-gated repo with no ungated distribution),
   because the Engine's one network call holds no credential and never will
   (decision 5). Copyleft weights (Piper's GPL-3.0 relicence) also stay out,
   not because the bar's words exclude them but because their source-provision
   duty has no meaning for a tensor file and the ADR would be guessing at
   what it owes; they can come in by an amendment that says.

2. **The allowlist stays an allowlist, and it stays on the manifest schema.**
   ADR 0008 §2 and §3 stand unchanged. Curation may not invent a licence
   string, the list moves only by an ADR amending this one, and
   `test_the_allowlist_is_exactly_what_the_adr_admits` is what makes a widening a visible edit. The list is now:

   | Id | Licence | Readily must ship | Binds the reader |
   |---|---|---|---|
   | `MIT` | MIT | the notice | no |
   | `BSD-2-Clause`, `BSD-3-Clause` | BSD | the notice | no |
   | `CC0-1.0` | CC0 | nothing | no |
   | `Apache-2.0` | Apache 2.0 | the licence (§4(a)); modified-file notices if Readily requantises (§4(b)); upstream's NOTICE if it ships one (§4(d)) | no |
   | `CC-BY-4.0` | Creative Commons Attribution 4.0 | creator, copyright notice, licence reference, warranty notice, link to the source, and a modified indication if Readily requantises (§3(a)(1)); the licence names a hyperlink to a page carrying them as a reasonable way to satisfy that (§3(a)(2)) | no; §3(a) triggers only "If You Share", and running is unconditioned |
   | `llama3.1`, `llama3.2`, `llama3.3`, `llama4` | Llama Community License | the Agreement (§1.b.i(A)); a `Notice` file with Meta's attribution line, which differs per version (§1.b.iii); "Built with Llama" displayed in the UI or product docs (§1.b.i(B)); "Llama" at the start of any derived model's name (§1.b.i) | **yes.** The preamble binds anyone who uses the Materials, and §1.b.iv incorporates the Acceptable Use Policy into the Agreement. §1.b.ii exempts the end user of an integrated product from §2 (the 700M-MAU clause) and from nothing else |
   | `bigscience-openrail-m`, `creativeml-openrail-m` | OpenRAIL-M | the licence (§4(b)); modified-file notices (§4(c)); all upstream notices retained (§4(d)) | **yes.** §4(a): the Attachment A restrictions "MUST be included as an enforceable provision by You in any type of legal agreement"; §5: "You shall require all of Your users … to comply" |

   The Gemma Terms of Use (Gemma 1 to 3: the Agreement, a `Notice` file
   and modified-file notices under §3.1, and binding on the reader) were on
   this list as `gemma`, and were dropped before any entry used the id:
   Google has revised the Terms since the Feb 2024 text, so one unversioned
   id could hand a reader the wrong agreement, and Gemma 4 is Apache-2.0.
   When a Gemma-licensed model is actually curated, the id must be
   versioned the way `llama3.x` is, each with the text it names.

   Ids are SPDX where SPDX has one and Hugging Face's `license` tag where it
   does not, because the model card is where curation reads them. The
   licence on the *weights* is what is recorded. A repository's licence tag
   describes the repository: Pocket TTS's inference code is MIT, and its
   every published weights distribution is CC-BY-4.0. Curation
   reads the weights' own licence file, as ADR 0008 already required.

3. **The licence travels with the weights, and the app shows it.** Ollama's
   artifact format carries the licence as a
   first-class layer (`application/vnd.ollama.image.license`), so `ollama
   pull gemma3` writes the full Terms of Use to disk next to the weights and
   `ollama show --license` prints them. Compliance is a property of the
   format, not of whoever wrote the entry. Readily's equivalent has three
   parts. The app bundles the canonical text of every licence on the list,
   keyed by id, so an Apache-2.0 entry whose upstream ships no LICENSE file
   (Qwen, Mistral) still hands the reader §4(a)'s copy. And an entry pins
   upstream's own `LICENSE` and `NOTICE` files in `files` when the repo ships
   them, so they are hash-verified, promoted, and deleted with everything
   else. Copyright lines written beside those terms arrive from the source
   rather than from a curator's transcription. CC-BY attribution records
   its copyright notice separately because the stock licence text has no
   holder line.
   Third, the `Notice` files Llama §1.b.iii and Gemma §3.1 prescribe carry
   exact strings, so the store writes them itself, from the obligations
   table, into staging after `_keep_only_manifest_files` has pruned and
   before the directory is renamed into place, unless the entry pins
   upstream's own `Notice` (the bare stem, the name the licence spells), in
   which case curation has read it and refused any draft where it does not
   say exactly the prescribed string, and the pinned copy ships in its place:
   the store never writes over a pinned file. A `NOTICE.md` or `NOTICE.txt` is
   upstream's third-party notice, pinned as found and never stood in for the
   prescribed one. The store-written file is generated from the app's own
   table, not fetched, so it is outside `_verify`'s hash check by
   construction and ADR 0003 §3's invariant (a promoted directory is verified
   and complete) still names every byte's origin. The Catalog sheet, which
   already prints the licence name, links to the text.

   Curation recognises upstream's file by its complete terms, and what
   stands beside them — the holder's copyright lines, whatever fills the
   blanks MIT, BSD and Apache's appendix leave for a name — is not matched
   by any grammar: the curator reads the file and declares those lines in
   the entry's `copyright_notice`, and curation refuses the file unless it
   says exactly that there and nothing else, so a term written beside the
   terms has to get past a reviewer, not a heuristic. This is SPDX's
   licence-matching guidelines with one tightening: the terms must be whole
   and carry no additional text (B.3.3); text after their obvious end, such
   as a third-party notice under its own heading, is ignored (B.13); and
   where SPDX ignores the copyright notice altogether (B.11, the templates
   wildcard it), Readily compares it against the curator's declaration,
   because a wildcard there is exactly where a rider hides. Publishing
   platforms (the Hugging Face `license` tag, an Ollama Modelfile's
   `LICENSE`, a ModelPack's doc layer) verify nothing, and the detectors
   built for inventories (licensee at 98% similarity, ScanCode's coverage
   score) are fuzzy by design and would pass a one-sentence rider on Apache
   text, so neither is a model for a gate. The check is a curation-time lint
   against curator error, not a security boundary: an upstream that writes
   its restrictions into a README no licence matcher sees, and the threat
   model names the curation PR review as the mitigation. A new category of
   bypass is met by deleting recognition and keeping the declared id and the
   pinned hash, not by another rule.

4. **Licences that bind the reader are accepted by downloading, and the
   sheet says so.** There is no acceptance modal. Gemma §3.1 and
   OpenRAIL §4(a) still require an "enforceable provision in any agreement
   governing the use", and there is no agreement to put it in unless the
   download is one. So for an entry whose licence binds the reader, the
   sheet's detail line reads "downloading accepts the Llama 3.1 Community
   License", the licence text is one tap away, and the download action is
   the acceptance. Entries whose licence does not bind the reader carry no such
   line. Whether the licence binds the reader is a fact about the licence,
   not the entry, so it lives in a table keyed by licence id next to the
   allowlist, never as a field curation fills in. Ollama ships the ToU
   alone, with no acceptance and no mention in its own terms; Readily does
   not.

5. **Readily does not re-host, and the Engine takes no credential.** Ollama
   re-hosts every model in `registry.ollama.ai`, which is how it serves
   HF-gated Gemma 3 and Llama 3.1 blobs to an unauthenticated `HEAD`. LM
   Studio and Jan pull direct from Hugging Face. Readily stays with the
   latter. A gated model enters the Catalog only through an ungated
   distribution: an official reduced build, an `mlx-community` conversion,
   or a pinned re-upload whose provenance the curation PR records and
   defends, since a hobbyist re-export can meet ADR 0003's letter and not its
   point (community ONNX exports of Pocket TTS are the standing example). A
   user-supplied Hugging Face token was considered and refused: it would put
   a credential into the one sanctioned network call site, which today holds
   none, and a reader's token in the Engine is a secret the threat model
   would have to start protecting. Re-hosting was considered and refused
   for now: it makes Readily the distributor of record for every entry, with
   infrastructure to run and every redistribution duty above to discharge
   for every model. It is a separate decision with its own ADR if it ever
   comes.

6. **The dependency licence policy is untouched.** Nothing GPL/AGPL/LGPL is
   linked or imported into a shipped process, and cargo-deny, `licenses:js`
   and pip-licenses still enforce that. This ADR is about bytes the Engine
   downloads and never links. A Gemma or RAIL *runtime* (a G2P, a codec
   library) is a dependency and meets the dependency rule.

## Amendment: a reviewed source may supply a missing holder line

Some MIT and BSD weight repositories declare their licence but ship no
licence file. Refusing them does not make the licence clearer. Ollama takes
the same practical position: its [`bge-m3` MIT layer](https://ollama.com/library/bge-m3:latest/blobs/a406579cd136)
ships the stock text even though the holder line is still the template's
`[year] [fullname]` placeholder.

Readily takes one stricter step. If an MIT or BSD entry has no pinned licence
file, `copyright_source` records the page where the curator read the declared
`copyright_notice`. The store writes the licence title, that holder line, and
the bundled stock terms into `LICENSE`. The Catalog sheet shows the same text.
PR review checks both the line and its source. That is the same human check
decision 3 already uses for licence recognition, and the threat model already
names Catalog review as the mitigation for licence-lint limits.

If upstream ships a licence file, the original rule still applies. Curation
pins the file and refuses it unless both its licence and holder lines match the
entry. The pinned file then supplies the provenance, so `copyright_source`
must be null. The Engine never fetches a `copyright_source` URL.

The first entry under this amendment, `chatterbox:turbo`, also settles the
second-repository tokenizer that keeps `pocket-tts` out below. mlx-audio's
loader tries to fetch an unpinned `mlx-community/S3TokenizerV2` after the
weights load. The MLX lane loads with Hugging Face forced offline on its
thread, so that fetch fails locally and the tokenizer keeps the random
weights it was constructed with.
Narration never runs it: it serves voice cloning, which the lane does not
offer. The Catalog therefore pins nothing from that repository, whose
licence is untagged.

## What this does not settle

These are recorded so the next reader does not re-derive them or paper over
them.

- **Whether model weights are copyrightable at all** is unresolved (the US
  Copyright Office's AI report, parts 1 to 3, does not reach it). If they are
  not, every licence above binds only by contract, that is only whoever
  accepted it. Decision 4 is the attempt to make the reader a party either
  way; whether a sentence on a sheet plus a linked text does that is the next
  bullet but one.
- **Whether an HF gate click-through creates obligations beyond the licence
  text.** Decision 5 sidesteps it: curation never clicks a gate, it sources
  from an ungated distribution.
- **Whether Readily is a distributor at all.** Under decision 5 the reader's
  own machine fetches from Hugging Face, which is LM Studio's and Jan's
  posture, and on one reading a client that never holds the bytes never
  "distributes", "Shares" or "reproduces" them, so none of the clauses in
  decision 2's table fire on Readily. Decision 1 is phrased as if they do,
  and decision 3 discharges every duty as if they do. That is a conservative
  choice against an unsettled characterisation, and it is cheap; it is not a
  finding that the clauses fire.
- **Whether "provide a copy of this Agreement" is satisfied by a hyperlink.**
  CC-BY §3(a)(2) says yes in terms. Gemma §3.1 and Llama §1.b.i(A) have no
  such clause. Decision 3 ships the bytes, which is the conservative reading
  and cheap: the largest licence text found was 12,320 bytes.
- **Whether a download-as-acceptance discharges Gemma §3.1's "enforceable
  provision".** The text asks for two things, an enforceable provision and a
  notice, and decision 4 provides both in the only agreement Readily has with
  a reader. A lawyer has not read this ADR.

## Consequences

- The redistributable-licence allowlist is `LICENCE_OBLIGATIONS` in
  `catalog/licence_table.py`: per licence, its display name, its `Notice`
  text if any, its UI attribution line if any, and whether it binds the
  reader. The manifest validator is
  `the_licence_is_one_readily_may_hand_a_reader`. The schema is B1, so a
  change to it takes the trust-boundary review.
- The wire (`docs/wire.md`) gives the sheet a way to reach the licence
  text, the binds-reader fact, and the CC-BY attribution facts the Manifest
  records for readers: creator, copyright notice, and whether Readily
  modified the files. The wire joins those to the licence's warranty notice
  and the pinned source page. The curation `provenance` note stays
  off the wire. The sheet's `describeDetail()` line carries the
  "downloading accepts" clause.
- `pocket-tts` is not refused on licence. It is still not
  shippable, for four reasons that survive any licence ruling: the official repo is gated, there is no upstream
  ONNX export, the ONNX lane would need a new autoregressive loader threading
  74 state tensors, and the MLX conversion fetches its tokenizer from a
  second repo outside the hash-verified store.
- The practical exclusion list is now short. Sampled from Ollama's library,
  `gemma4`, `qwen3`, `mistral`, `granite4` and `nomic-embed-text` are
  Apache-2.0 and `phi4` and `deepseek-r1` are MIT; the community-licensed
  remainder is admitted by decision 2. What stays out is non-commercial and
  research-only scopes, and anything only reachable behind a gate.
