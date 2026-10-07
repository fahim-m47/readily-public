import { useState } from "react";
import type { CatalogLicense, ReferenceLicense } from "../engine/client";
import { tryOpenInBrowser } from "./browser";
import Sheet from "./Sheet";

export type LicenceViewProps = {
  entryName: string;
  terms: CatalogLicense;
  // The licences the entry's Voice Reference clips are under, for clips
  // cut from a consented corpus; shown beneath the weights' own terms.
  referenceLicenses?: ReferenceLicense[];
  onClose: () => void;
};

// A page the credit points at, opened in the reader's browser; when the
// host will not open it the address stays on screen to be copied.
function SourceLink({ source }: { source: string }) {
  const [wouldNotOpen, setWouldNotOpen] = useState(false);
  return (
    <>
      <a
        className="licence__source"
        href={source}
        onClick={(event) => {
          event.preventDefault();
          void tryOpenInBrowser(source).then((opened) =>
            setWouldNotOpen(!opened),
          );
        }}
      >
        {source}
      </a>
      {wouldNotOpen && (
        <span className="licence__note">
          That did not open here — the address can be copied.
        </span>
      )}
    </>
  );
}

export default function LicenceView({
  entryName,
  terms,
  referenceLicenses = [],
  onClose,
}: LicenceViewProps) {
  const attribution = terms.attribution;

  return (
    <Sheet
      title={terms.name}
      variant="licence"
      onClose={onClose}
    >
      <p className="sheet__lede">
        The licence {entryName} is under. A downloaded model keeps a copy
        beside its files on disk, deleted with them.
      </p>

      {attribution && (
        <dl className="licence__facts">
          <dt>Creator</dt>
          <dd>{attribution.creator}</dd>
          <dt>Copyright</dt>
          <dd>{attribution.copyrightNotice}</dd>
          <dt>Source</dt>
          <dd>
            <SourceLink source={attribution.source} />
          </dd>
          <dt>Warranty</dt>
          <dd>{attribution.warrantyNotice}</dd>
          {attribution.modified && (
            <>
              <dt>Changes</dt>
              <dd>Modified by Readily.</dd>
            </>
          )}
        </dl>
      )}

      <pre className="licence__text">{terms.text}</pre>

      {referenceLicenses.map((licence) => {
        const heading = `Voice clips under ${licence.name}`;
        return (
          <section
            key={licence.id}
            className="licence__clips"
            aria-label={heading}
          >
            <h3 className="licence__clips-title">{heading}</h3>
            <p className="licence__clips-lede">
              The recordings these Voices are cloned from were cut from a
              consented speech corpus and are credited here.
            </p>
            {licence.clips.map((clip) => (
              <dl key={clip.voice} className="licence__facts">
                <dt>Voice</dt>
                <dd>{clip.voice}</dd>
                <dt>Creator</dt>
                <dd>{clip.creator}</dd>
                <dt>Copyright</dt>
                <dd>{clip.copyrightNotice}</dd>
                <dt>Source</dt>
                <dd>
                  <SourceLink source={clip.source} />
                </dd>
                {clip.modified && (
                  <>
                    <dt>Changes</dt>
                    <dd>Modified by Readily.</dd>
                  </>
                )}
              </dl>
            ))}
            <dl className="licence__facts">
              <dt>Warranty</dt>
              <dd>{licence.warrantyNotice}</dd>
            </dl>
            <pre className="licence__text">{licence.text}</pre>
          </section>
        );
      })}
    </Sheet>
  );
}
