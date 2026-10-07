"""The Engine's one allowlisted egress package.

Every network call the Engine ever makes lives here, and there are two:
Voice Model downloads via `huggingface_hub`, pinned to immutable revisions
(`fetch`, threat model egress inventory row 1), and the one page a reader
asks to read through "Open link" (`link`, row 5), fetched with the standard
library under an SSRF guard. The Semgrep no-egress rule allowlists exactly
this path; importing a network-capable library anywhere else in the Engine
fails CI. Downloads land in the store's staging area and are handed to
`readily_engine.store` for verification — this package never promotes. A
fetched page is handed straight back to the webview and never stored.
"""
