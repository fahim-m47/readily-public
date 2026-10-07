# Context

Glossary of the ubiquitous language for Readily. Terms are added as they crystallise; no implementation details live here.

## Terms

### Readily
The product: a local, open-source Mac app that narrates text with downloadable open-source TTS models.

### Reader
The person Readily narrates to: the end user who downloads a Voice Model and runs it on their own machine. Used where a licence reaches past Readily to that person (ADR 0009).

### Voice Model
A downloadable open-source TTS model from the Catalog. A Voice Model may offer one or more voices.

### Support Model

A downloadable model that helps a Voice Model produce a Narration without speaking itself. A forced aligner is a Support Model that matches known words to generated audio.

### Catalog
The curated, known-good list of Voice Models Readily can download and run. Curated — not "any model on the internet".

### Catalog Manifest
The definitive description of the Catalog, shipped inside each app release: for every Voice Model, its identity, files and how to verify them, license, Tier, and Voices. The Engine downloads and runs only what the Manifest names.

### Tier
The Catalog's editorial label for what a Voice Model is for: `instant` or `expressive`. User-facing vocabulary — it is the label the picker renders. Curated in the Manifest, not computed — and deliberately not a scale; a rating ladder can return if the Catalog ever needs one.

### Architecture
The code that runs one kind of Voice Model: one package under the Engine's `loading/`, or one spec file in a family package whose exports share a synthesizer (`loading/styletts2/`), registered under an id the Catalog entry names in its `architecture` field. It declares what its consumers need — the files it expects, how a Voice conditions it, its warm-up text, its parameter schema and the budgets curation may sweep — and turns a promoted model directory into a synthesizer (ADR 0014). Two entries can share an Architecture; a weights bump never changes it.
_Avoid_: "lane" (that is the Backend) or "export" for the code, and keying anything on an entry's `name` to find it

### Backend
Which runtime lane an Architecture runs on: `onnxruntime` or `mlx-audio`. Each Architecture declares it; the user never sees it — unlike Tier, it stays off the wire.

### Voice
One named speaking identity offered by a Voice Model. Presets only for now — cloning is out of scope.

### Voice Preview
A short sample clip of a Voice, created when its Voice Model enters the Catalog and shipped with the app, so any Voice can be auditioned before anything is downloaded.

### Voice Reference
A short clip of a Voice and the exact words it says, named by the Catalog and shipped with the app, that the Engine conditions every Block on so the Voice holds its pitch, register and pace across a Narration. Only Voice Models whose speaker table is empty need one. Read by the Engine, never by the UI — unlike a Voice Preview, its audio stays off the wire. Only its credit crosses, for a clip cut from a corpus, so the entry's licence sheet can name who recorded it.

### Audition
Playing a Voice Preview to hear a Voice before choosing it. An audition reads a clip out of the app bundle, so it works with the network off and with nothing downloaded.

### Engine
The local service that downloads, verifies, and runs Voice Models to produce Narrations. The UI never runs a model itself; it asks the Engine.
_Avoid_: sidecar, server, runtime (implementation postures, not the concept)

### Narration
One audio output: the result of narrating a Source with a Voice Model. Narrations accumulate in History.

### Source
The text a Narration was generated from: typed, pasted, or the text of a `.txt`, `.md`, Word (`.docx`), EPUB or PDF file dropped on or opened into the composer, or the article on a web page the Reader opened by its link.

### Block
A contiguous run of a Source's text, cut at natural speech boundaries (never crossing a paragraph break), sized so a Voice Model can narrate it in one pass.

### Segment
The audio the Engine produces for one Block. A Narration's audio is its Segments played in order.

### Generation Record
The complete, frozen description of how a Block becomes a Segment: its words, Voice Model and Catalog version, Voice and Voice Reference, decoding and sampling choices, and seed. Its identity belongs to the content, independent of where the Block appears or how fast a reader plays it.

### Take
One of the Segments the Engine has kept for a Block, named by its Generation Record. A Block keeps two: A, its original, and B, its latest re-roll, so a reader can hear both and choose which the Narration keeps.

### Pause Policy
The assembly settings a Narration is stitched with: the silence kept around each Segment and the pauses between Blocks, under a version number. A Catalog entry carries the current one; a Narration saves its own, so a later Catalog change never moves a saved timeline.

### Control
One input a Catalog entry lets the Reader change: a sampling or decoding choice the entry declares, or one of the shared assembly and seeding controls every entry offers. A control's default is the entry field it names, so an entry never declares a control it does not itself set.

### Override
The Reader's value for one Control, saved per entry and Voice. Composing a Voice's overrides over its entry yields the resolved entry a Narration is planned from. Overrides outlive the Catalog that accepted them, so one a later entry no longer admits is dropped and the default returns.

### Recipe
The shape of a Voice's Generation Record together with its entry's tunables: everything a digest pins so a Narration can be told apart from one made under other settings.

### Qualification
The pinned `recipe_sha256` in the Catalog Manifest that says a curator checked a Voice's Recipe. A changed default breaks the match until a curator checks the Recipe again. It is a record, not a gate: every Mode offers every Voice.

### Mode
Simple or Advanced, chosen in the shell and carried on narrate. Both offer every Voice. Simple resolves the Catalog defaults; Advanced applies the Reader's Overrides.

### Playback Speed
How fast the Reader hears a Narration: 0.5x through 4x in either Mode. Above 3x, speech stays stretched at 3x while authored pauses get shorter; the effective rate depends on how much of the Narration is pauses. One setting for the whole app, persisted and applied live on the way to the output. It never changes a Segment, a Generation Record, or a saved timeline; positions and Export stay in source time.

### History
The reverse-chronological record of all Narrations. An entry is permanent until the user deletes it — a Narration's audio may be cleaned up to save disk, but its record and Source remain, and its audio can always be re-made. A Narration stopped before any of its audio was made never becomes an entry.

### Export
Writing a finished Narration to a single audio file in Readily's audio folder, `Readily` in the Reader's Documents. The file is named from the Source and never replaces one already there; Settings opens the folder in the Finder. The one way Narration audio leaves Readily's own storage.

### Update
A newer Readily build. The shell asks the Update Endpoint once at launch, offers what it finds, and installs only when the Reader says so.

### Update Endpoint
The one URL, fixed at build time, where the shell asks for `latest.json`. The only request Readily makes without being asked.

### Install Site
The running `Readily.app` itself, wherever it sits. An Update replaces it by renaming it in place, so one is offered only when the folder around the Install Site is writable and the Install Site is not Gatekeeper's translocated copy.

### Favorite (v1)
A Narration the user has bookmarked. Favorites can be organised into Folders in the sidebar.
