"""PCM cleanup and Voice Model qualification for the Readily Engine."""

import numpy as np
import numpy.typing as npt


class AudioOutputUnavailable(RuntimeError):
    """The active output stopped accepting audio for this Narration."""


MIN_PLAYBACK_SPEED = 0.5
MAX_PLAYBACK_SPEED = 4.0
MAX_STRETCH_SPEED = 3.0


def validate_playback_speed(speed: float) -> float:
    """Return a finite speed inside the range qualified for playback."""
    if not np.isfinite(speed) or not MIN_PLAYBACK_SPEED <= speed <= MAX_PLAYBACK_SPEED:
        raise ValueError(
            f"Playback speed must be between {MIN_PLAYBACK_SPEED} and "
            f"{MAX_PLAYBACK_SPEED}"
        )
    return speed


# Mono float32 PCM, the one sample format audio passes between Engine modules.
FloatPcm = npt.NDArray[np.float32]
