import { describe, expect, it } from "vitest";

import { DISCLOSURES, DISCLOSURE_SUMMARY } from "../src/disclosures";

describe("store & in-app disclosures", () => {
  it("covers recording, biometric, self-hosting and retention (master plan §9)", () => {
    const ids = DISCLOSURES.map((d) => d.id);
    expect(ids).toEqual(expect.arrayContaining(["recording", "biometric", "self-hosted", "retention"]));
  });

  it("gives every disclosure a non-empty title and body", () => {
    for (const d of DISCLOSURES) {
      expect(d.title.length).toBeGreaterThan(0);
      expect(d.body.length).toBeGreaterThan(0);
    }
  });

  it("names the biometric statutes review checks for in the biometric disclosure", () => {
    const bio = DISCLOSURES.find((d) => d.id === "biometric");
    expect(bio?.body).toMatch(/BIPA/);
    expect(bio?.body).toMatch(/GDPR/);
  });

  it("summary mentions voiceprints/biometrics for the store listing", () => {
    expect(DISCLOSURE_SUMMARY.toLowerCase()).toMatch(/voiceprint|biometric/);
  });
});
