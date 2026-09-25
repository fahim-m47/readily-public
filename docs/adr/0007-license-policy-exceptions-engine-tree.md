# License-policy rulings for the Engine's synthesis tree

Status: accepted (2026-08-26) · Builds on [ADR 0005](0005-license-policy-exceptions-tauri-tree.md)

The first synthesis slice gives the Engine its first real synthesis dependencies —
misaki (G2P), spaCy (misaki's tokenizer), onnxruntime, and sounddevice — and
their transitive tree forces the same two kinds of ruling ADR 0005 made for
the Tauri tree: new spellings of permissive licenses, and a scoped copyleft
exception.

## The decisions

1. **Permissive spellings and composites join the allow list.** Package
   metadata spells the same permissive licenses many ways; the gate's allow
   list carries each spelling explicitly rather than pattern-matching.
   Newly admitted: `MIT-0` (cffi), `3-Clause BSD License` (protobuf),
   `Apache-2.0 OR BSD-2-Clause` (packaging — a disjunction of two allowed
   licenses), and `Apache-2.0 AND CNRI-Python` (regex — CNRI-Python is the
   OSI-approved permissive Python 1.6 license). Every term in every admitted
   expression is permissive; a composite containing any non-permissive term
   stays denied.
2. **MPL-2.0 is excepted for exactly two packages: `certifi` and `tqdm`.**
   Both arrive through spaCy's hard dependencies (requests → certifi;
   tqdm directly) and cannot be dropped without forking misaki's tokenizer
   stack. As in ADR 0005: MPL-2.0 is file-scoped weak copyleft, obligations
   attach only to modifying those files, and we ship both unmodified.
   (tqdm is `MPL-2.0 AND MIT`; the MPL half is what needs the ruling.)
   The exception is scoped per-package via `--ignore-packages` in the CI
   gate — MPL-2.0 stays default-deny for anything new.
3. **The LGPL `num2words` is refused, not excepted.** misaki's English G2P
   calls one function from the LGPL num2words distribution. Rather than
   admit LGPL into the shipped Engine, `engine/src/num2words/` provides an
   MIT-licensed implementation of that narrow surface (cardinal, ordinal,
   year modes), and the real distribution is absent from the lockfile.
   `engine/tests/test_number_words_compat.py` pins the compatibility
   surface.

## Consequences

- `.github/workflows/ci.yml`'s "Engine production license gate" is the
  enforcement point; its `--ignore-packages` list must reference this ADR
  and stay limited to the two packages above.
- A future Engine dependency bringing MPL-2.0 (or any other copyleft) fails
  CI until a new ruling lands here.
- If misaki ever grows a hard dependency on the real num2words, the lock
  step fails (`readily-engine` already builds its own `num2words` module)
  and the shim's adequacy must be re-examined rather than papered over.
