"""The Engine's one allowlisted egress package.

Every network call the Engine ever makes lives here — Voice Model downloads
via `huggingface_hub`, pinned to immutable revisions, nothing else (threat
model, egress inventory row 1). The Semgrep no-egress rule allowlists exactly
this path; importing a network-capable library anywhere else in the Engine
fails CI. Downloads land in the store's staging area and are handed to
`readily_engine.store` for verification — this package never promotes.
"""
