/**
 * App-store & in-app biometric/recording disclosures (master plan §9, risk #6).
 *
 * The mobile app stores conversation transcripts, audio and **voiceprints**
 * (biometric data) on a self-hosted server. Apple App Store and Google Play review
 * both require a clear, in-app disclosure of microphone use and biometric/recording
 * data handling before submission; §9 also calls for an explicit recording notice.
 * Phase 9 ships *browse/manage only* (no capture), but the app still surfaces these
 * so the posture is correct ahead of the Phase 10 capture client.
 *
 * Kept as plain data (no React Native imports) so the copy is unit-tested and reused
 * by the Disclosure screen, the login footer, and the store-listing metadata.
 */

export interface Disclosure {
  id: string;
  title: string;
  body: string;
}

export const DISCLOSURES: Disclosure[] = [
  {
    id: "recording",
    title: "Recording notice",
    body:
      "Conversations may be recorded and transcribed. You are responsible for " +
      "informing the people you speak with and for complying with the recording " +
      "and two-party-consent laws that apply where you are.",
  },
  {
    id: "biometric",
    title: "Biometric (voiceprint) data",
    body:
      "To recognise who is speaking, the server derives a voiceprint from enrolled " +
      "voices. Voiceprints are biometric data regulated by laws such as the EU GDPR " +
      "and the Illinois BIPA. They are stored only on your self-hosted server and " +
      "can be exported or deleted per person at any time.",
  },
  {
    id: "self-hosted",
    title: "Your own server",
    body:
      "This app talks only to the server URL you configure. Your audio, transcripts " +
      "and people data stay on that self-hosted instance and are never sent to a " +
      "third party by this app.",
  },
  {
    id: "retention",
    title: "Retention & deletion",
    body:
      "Retained audio is purged on the schedule your server sets, and you can delete " +
      "any conversation or person from this app. Deletion on the server is permanent.",
  },
];

/** One-line summary shown under the login form and on the store listing. */
export const DISCLOSURE_SUMMARY =
  "Records and transcribes conversations and stores voiceprints (biometric data) on " +
  "your own self-hosted server. Review the in-app disclosures before recording.";
