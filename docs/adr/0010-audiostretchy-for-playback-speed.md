# ADR 0010: AudioStretchy for playback speed

Status: accepted.

Use `audiostretchy==1.3.5` through its low-level TDHS interface. One handle
persists across Segments and live speed changes; seek starts a fresh handle.
The high-level file API is not used. Export keeps the original source rate.

The exact wheel was qualified by a local latency and listening comparison.
Its SHA-256 is `17f65d4e5e8f08c75d3add851edca5751a3925d7b46b92e65d81842d4bf10dc9`.
Python and bundled audio-stretch code are BSD-3-Clause. Mandatory dependencies
are NumPy, Fire (Apache-2.0), and Termcolor (MIT). No optional MP3 or resampling
extras are installed. The macOS library links only libSystem. Distribution
license notices remain in the installed wheel. This new Engine dependency
touches supply-chain boundary B5; existing locked provisioning applies.

Playback offers 0.5–4×.
