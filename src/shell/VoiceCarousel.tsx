import { useEffect } from "react";
import type { CatalogVoice } from "../engine/client";
import { ChevronLeft, ChevronRight, Pause, Play } from "lucide-react";
import { VoiceOrb } from "./orb/VoiceOrb";
import type { OrbIdentity } from "./orb/identity";
import { previewUrl } from "./catalog";
import { useAudition } from "./useAudition";

export type VoiceCarouselProps = {
  modelId: string;
  // The chosen Voice Model's Voices, in the Catalog's order.
  voices: readonly CatalogVoice[];
  voiceId: string;
  // The orb for one of these Voices, from the whole Catalog rather than
  // the list above, which a mode may have thinned.
  orb: (voiceId: string) => OrbIdentity;
  onSelect: (voiceId: string) => void;
  // Smaller orbs once the composer holds text, so the words get the room.
  compact: boolean;
  // Something modal is over the carousel, so no clip may keep playing here.
  silenced: boolean;
};

// How many orbs sit either side of the chosen one, and their sizes from the
// centre outwards. Fewer Voices means fewer orbs, never repeats.
const WINGS = 2;
const SIZES = { full: [144, 88, 64], compact: [88, 56, 44] } as const;

// The Voice picker: the chosen Voice's orb in the middle, its neighbours
// fading either side, and a play button to hear it before narrating.
//
// Every slot is the same element with the orb first and its control laid
// over it, so stepping the carousel re-sizes each orb rather than
// remounting it: a remount is a fresh WebGL context, and the browser caps
// those at about sixteen.
export default function VoiceCarousel({
  modelId,
  voices,
  voiceId,
  orb,
  onSelect,
  compact,
  silenced,
}: VoiceCarouselProps) {
  const audition = useAudition();
  const key = `${modelId}/${voiceId}`;
  const { playing: playingKey, stop } = audition;
  useEffect(() => {
    if (playingKey !== null && (playingKey !== key || silenced)) stop();
  }, [key, playingKey, silenced, stop]);

  const chosen = Math.max(0, voices.findIndex((voice) => voice.id === voiceId));
  const voice = voices[chosen];
  if (voice === undefined) return null;

  const url = previewUrl(voice);
  const playing = audition.playing === key;
  const many = voices.length > 1;
  const sizes = SIZES[compact ? "compact" : "full"];
  const step = (by: number) =>
    onSelect(voices[(chosen + by + voices.length) % voices.length].id);

  const wings = many ? Math.min(WINGS, Math.floor((voices.length - 1) / 2)) : 0;
  const offsets = Array.from({ length: wings * 2 + 1 }, (_, at) => at - wings);

  return (
    <div className={`carousel${compact ? " carousel--compact" : ""}`}>
      <div className="carousel__orbs">
        {offsets.map((offset) => {
          const neighbour = voices[(chosen + offset + voices.length) % voices.length];
          const distance = Math.abs(offset);
          const centre = offset === 0;
          return (
            <span className={`carousel__slot carousel__slot--${distance}`} key={neighbour.id}>
              <VoiceOrb
                {...orb(neighbour.id)}
                animated={centre && playing}
                label={centre ? `${neighbour.name} orb` : undefined}
                level={centre && playing ? audition.level : 0}
                size={sizes[distance]}
                speed={centre && playing ? 0.6 : 0.35}
              />
              {!centre && (
                <button
                  aria-label={`Choose ${neighbour.name}`}
                  className="carousel__side"
                  onClick={() => onSelect(neighbour.id)}
                  type="button"
                />
              )}
              {centre && url !== null && (
                <button
                  aria-label={playing ? `Stop ${neighbour.name}` : `Hear ${neighbour.name}`}
                  aria-pressed={playing}
                  className="carousel__hear"
                  onClick={() => audition.toggle(key, url)}
                  type="button"
                >
                  {playing ? <Pause size={14} fill="currentColor" strokeWidth={0} /> : <Play size={14} fill="currentColor" strokeWidth={0} />}
                </button>
              )}
            </span>
          );
        })}
      </div>

      <div className="carousel__name">
        {many && (
          <button
            aria-label="Previous voice"
            className="carousel__step"
            onClick={() => step(-1)}
            type="button"
          >
            <ChevronLeft size={16} />
          </button>
        )}
        <span className="carousel__voice" aria-live="polite">
          {voice.name}
        </span>
        {many && (
          <button
            aria-label="Next voice"
            className="carousel__step"
            onClick={() => step(1)}
            type="button"
          >
            <ChevronRight size={16} />
          </button>
        )}
      </div>

      {audition.failed === key && (
        <p className="carousel__note">This voice's preview could not be played.</p>
      )}
    </div>
  );
}
