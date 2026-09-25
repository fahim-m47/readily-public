import { useId } from "react";

type WordmarkProps = {
  // Height of the mark in px; the width follows from the artwork's ratio.
  height?: number;
};

// The `readily` wordmark: the last three letters are bound volumes on a
// shelf, with the dot of the `i` sitting above them.
//
// The mark is a single ink — everything takes `currentColor`, so it is the
// colour of the text around it. The binding lines are cut out of the spines
// with a mask rather than painted in the background colour, which is what
// lets the mark sit on any surface the shell has.
export default function Wordmark({ height = 22 }: WordmarkProps) {
  const bindings = useId();

  return (
    <svg role="img" aria-label="Readily" height={height} viewBox="0 0 470 184">
      <mask id={bindings} maskContentUnits="userSpaceOnUse">
        <rect x="280" y="0" width="110" height="184" fill="#fff" />
        <path
          d="M297 33h10 M297 39h10 M297 136h10 M328 82h10 M328 88h10 M328 136h10 M361 33h10 M361 39h10 M361 136h10"
          fill="none"
          stroke="#000"
          strokeWidth="2.5"
          strokeLinecap="round"
        />
      </mask>
      <g fill="none" stroke="currentColor" strokeWidth="12" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 146V70 M12 96Q20 65 46 70" />
        <path d="M67 105H125C127 55 61 57 61 107c0 44 45 51 64 26" />
        <path d="M212 70v76 M212 105c0-51-68-46-68 3 0 50 68 52 68-3" />
        <path d="M302 106c0-50-67-49-67 1 0 50 67 52 67-1" />
        <path d="M397 70l33 73 M457 70l-36 91q-7 19-24 16" />
      </g>
      <g fill="currentColor" mask={`url(#${bindings})`}>
        <rect x="292" y="17" width="20" height="135" rx="4" />
        <rect x="323" y="67" width="20" height="85" rx="4" />
        <rect x="356" y="17" width="20" height="135" rx="4" />
        <circle cx="333" cy="43" r="8" />
      </g>
    </svg>
  );
}
