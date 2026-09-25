# Voice Preview clips

The bundled curation artifacts that let any Voice be auditioned before its
Voice Model is downloaded (ADR 0003's consequences: "the catalog browser
works fully offline: voice metadata and bundled Voice Preview clips …
~4–8MB").

They live in `public/previews/`, which Vite copies into the bundle verbatim
— this document is here rather than there so it does not ship inside the app.

They are **app resources, not Engine ones**. The Catalog sheet loads them
from that directory over the webview's own origin — no Engine route, no
network call — which is what makes an audition work with the network off and
nothing on disk.

## Layout

One `.m4a` per Voice, at the path the Catalog Manifest's `preview` field
names for that Voice, relative to `public/previews/`:

```
public/previews/<model-name>/<tag>/<voice-id>.m4a
```

A Voice whose `preview` is `null` has no clip yet, and the sheet says so
rather than guessing at a path.

## Head and tail

A clip is not the capture verbatim. The stream curation qualifies is trimmed
hard to the speech, so a preview encoded from it would begin and end on a
step and an audition would open and close with a click. Before encoding,
`dress_for_audition` (`engine/src/readily_engine/curation/capture.py`) pads
200ms of silence in front of the speech and 400ms behind it, and fades the
speech in over 15ms and out over 80ms.

This dressing is preview-only. It is applied at the encode site, so the
qualification capture the analyzer reads — and every Narration — keeps the
Assembler's own edges, which are what ADR 0002 and qualification check B
measure. Nothing here changes how a Voice sounds inside a Narration.

## Generating them

They are not authored by hand — they fall out of curation, which is what
keeps a clip honest about the Voice it advertises. `readily-curate`
(`engine/README.md` § "Curate a Voice Model") narrates one standard passage
through the entry's real Backend in every Voice it lists, qualifies the
result, and only then encodes the clips here and writes each `preview` path
into the manifest:

```sh
cd engine && uv run readily-curate kokoro:82m --write
```

So a clip and the hashes beside it in the manifest always come from the same
run against the same bytes. Re-running an unchanged entry re-encodes the same
audio; commit the clip only when its entry changed, since AAC output is not
bit-reproducible across encoder versions.
