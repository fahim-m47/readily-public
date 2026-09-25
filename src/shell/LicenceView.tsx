import { useState } from "react";
import type { CatalogLicense } from "../engine/client";
import { tryOpenInBrowser } from "./browser";
import Sheet from "./Sheet";

export type LicenceViewProps = {
  entryName: string;
  terms: CatalogLicense;
  onClose: () => void;
};

export default function LicenceView({
  entryName,
  terms,
  onClose,
}: LicenceViewProps) {
  const [sourceWouldNotOpen, setSourceWouldNotOpen] = useState(false);

  const attribution = terms.attribution;

  return (
    <Sheet
      title={terms.name}
      variant="licence"
      onClose={onClose}
    >
      <p className="sheet__lede">
        The licence {entryName}&rsquo;s files are under. It is kept beside
        them on disk, and deleted with them.
      </p>

      {attribution && (
        <dl className="licence__facts">
          <dt>Creator</dt>
          <dd>{attribution.creator}</dd>
          <dt>Copyright</dt>
          <dd>{attribution.copyrightNotice}</dd>
          <dt>Source</dt>
          <dd>
            <a
              className="licence__source"
              href={attribution.source}
              onClick={(event) => {
                event.preventDefault();
                void tryOpenInBrowser(attribution.source).then((opened) =>
                  setSourceWouldNotOpen(!opened),
                );
              }}
            >
              {attribution.source}
            </a>
            {sourceWouldNotOpen && (
              <span className="licence__note">
                That did not open here — the address can be copied.
              </span>
            )}
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
    </Sheet>
  );
}
