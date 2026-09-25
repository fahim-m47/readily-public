# Bundled English pronunciation

Readily ships these files with the Engine for Misaki's unknown-word fallback.
They add 64,187,642 bytes of model data to the app and require no separate
hosting, account or download. Kokoro and Kitten share one fallback, built before either Voice Model loads.

The source is NRC-CNRC/en_us_cmudict_ipa_forward_g2p at revision
`05f9e2f78b12f4f8852e4f8b3f77dc7e01749293`, file
`en_us_cmudict_ipa_forward.pt`, SHA-256
`ee18c945608c0f2a52a4dc3edb21546a161911bde86be19052ff9895cec2483f`.

The [NRC model card](https://huggingface.co/NRC-CNRC/en_us_cmudict_ipa_forward_g2p/blob/05f9e2f78b12f4f8852e4f8b3f77dc7e01749293/README.md)
declares MIT and credits the original DeepPhonemizer authors. The copyright
notice comes from their [MIT licence](https://github.com/spring-media/DeepPhonemizer/blob/5dce7e2/LICENSE).
The lexicon inside that checkpoint is the CMU Pronouncing Dictionary
(cmudict 0.7b, BSD-2-Clause) transcribed to IPA by the `cmudict-ipa` project;
every entry and transcription matches upstream once stress is stripped, so
Carnegie Mellon's notice travels alongside the MIT one. Both notices and
their terms are in `LICENSE` beside these files.

`engine/tools/export_pronunciation.py` converts the checkpoint to a single
opset-18 ONNX graph and a deterministic 123,892-entry JSON lexicon. It checks
22 words against Torch, including 1- and 84-character inputs, and compares
the complete lexicon. `engine/tools/README.md` documents the isolated export
environment. Neither Torch nor the checkpoint ships with the app.

The runtime pins both SHA-256s in `loading/pronunciation.py` and checks them
before JSON parsing or ONNX loading. Tests check the committed data, and the
macOS bundle check compares the shipped directory byte for byte.
