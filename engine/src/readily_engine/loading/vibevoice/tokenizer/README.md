# Bundled VibeVoice text tokenizer

VibeVoice-Realtime reads its text through Qwen2.5-0.5B's byte-level BPE
tokenizer, and neither Microsoft's checkpoint nor mlx-community's conversion
carries it: mlx-audio's `post_load_hook` fetches `Qwen/Qwen2.5-0.5B` from the
Hub at load time. Loading never goes to the network (threat model B2), so
Readily ships the three files that tokenizer is built from with the Engine,
adding 4,455,900 bytes to the app.

The source is Qwen/Qwen2.5-0.5B at revision
`060db6499f32faf8b98477b0a26969ef7d8b9987`, files `vocab.json`,
`merges.txt` and `tokenizer_config.json`, copied unmodified. They build the
same `Qwen2Tokenizer`, with the same ids, as that revision's `tokenizer.json`
does. `LICENSE` beside them is that revision's Apache License 2.0, copyright
2024 Alibaba Cloud.

The runtime pins all three SHA-256s in `loading/vibevoice/__init__.py` and
checks them before building the tokenizer. The macOS bundle check compares
the shipped directory byte for byte.
