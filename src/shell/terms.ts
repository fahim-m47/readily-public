// The reader's one acceptance of every Voice Model's licence (ADR 0009 §4).
//
// Accepted once, on the launch screen, for the whole Catalog rather than per
// download: every way into a download (the sheet, the Voice Model menu, the
// first run's own fetch) comes after it. What is remembered is which licences
// were on the screen, so a later Catalog that brings a new one asks again. It
// lives in the webview's own storage beside the Voice memory.

import type { CatalogEntry, CatalogLicense } from "../engine/client";

const KEY = "readily.acceptedLicences";

// One licence the Catalog's files are under, and the models that carry it.
export type Licence = { terms: CatalogLicense; models: string[] };

// Every licence in the Catalog once, support models included, in the order
// the Catalog first names them. Models share a row only when their terms are
// identical: two under the same licence id can still carry their own holder
// line or attribution, and each must be readable as its model has it.
export const licencesOf = (entries: readonly CatalogEntry[]) => {
  const byTerms = new Map<string, Licence>();
  const carries = (name: string, terms: CatalogLicense) => {
    const key = JSON.stringify(terms);
    const licence = byTerms.get(key) ?? { terms, models: [] };
    licence.models.push(name);
    byTerms.set(key, licence);
  };
  for (const entry of entries) {
    carries(entry.name, entry.licenseTerms);
    for (const support of entry.supportModels) carries(support.name, support.licenseTerms);
  }
  return [...byTerms.values()];
};

export const acceptedLicences = (): string[] => {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter((id) => typeof id === "string") : [];
  } catch {
    return [];
  }
};

export const acceptLicences = (ids: readonly string[]) => {
  try {
    localStorage.setItem(KEY, JSON.stringify([...new Set([...acceptedLicences(), ...ids])]));
  } catch {
    // Storage refused: this launch goes ahead, and the next one asks again.
  }
};
