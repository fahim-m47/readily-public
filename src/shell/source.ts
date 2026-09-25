// A Source read the way the Engine indexes it.
//
// Every offset the Engine sends — a Segment's `sourceStart`/`sourceEnd`, a
// gap's range — is a Python string index, so it counts Unicode code points,
// under the Engine's invariant `source[block.start : block.end] ==
// block.text`. A JavaScript string counts UTF-16 code units instead, so one
// astral character slides every offset after it. This view keeps the two
// alike.
export type SourceText = {
  /** How many code points the Source has, which is what the Engine counts. */
  readonly length: number;
  /** The characters lying between two of the Engine's offsets. */
  readonly slice: (start: number, end?: number) => string;
};

export const sourceText = (source: string): SourceText => {
  const points = [...source];

  return {
    length: points.length,
    slice: (start, end) => points.slice(start, end).join(""),
  };
};
