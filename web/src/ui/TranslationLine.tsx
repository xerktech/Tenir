/**
 * English translation of a non-English turn (XERK-160), rendered turn-by-turn
 * directly under the original text. Quieter than the spoken words — it is a
 * rendering of the turn, not part of the record — with a small "EN" tag so the
 * pairing reads at a glance. Shared by the live transcript and history detail.
 */

const LANG_NAMES: Record<string, string> = {
  en: "English",
  es: "Spanish",
  fr: "French",
  de: "German",
  pt: "Portuguese",
  it: "Italian",
};

export function TranslationLine({
  text,
  lang,
}: {
  text: string;
  /** Language the original turn was spoken in, when detected. */
  lang?: string | null;
}): JSX.Element {
  const from = lang ? LANG_NAMES[lang] : undefined;
  return (
    <div className="translation">
      <span className="translation-lang" title={from ? `Translated from ${from}` : "Translated"}>
        EN
      </span>
      <span className="translation-text">{text}</span>
    </div>
  );
}
