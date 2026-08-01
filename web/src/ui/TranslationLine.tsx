/**
 * English translation of a non-English turn (XERK-160), rendered turn-by-turn
 * directly under the original text. Quieter than the spoken words — it is a
 * rendering of the turn, not part of the record — with a small "EN" tag so the
 * pairing reads at a glance. The original turn carries the matching source
 * tag (`LangTag`, e.g. "ES") so both halves of the pair are labeled.
 */

const LANG_NAMES: Record<string, string> = {
  en: "English",
  es: "Spanish",
  fr: "French",
  de: "German",
  pt: "Portuguese",
  it: "Italian",
};

/**
 * The source-language chip on the ORIGINAL text of a translated turn — the
 * counterpart of the translation's "EN" tag, same chip style. Rendered only
 * when the turn's language was actually detected; a turn translated by run
 * inheritance has no claimed source language and stays untagged.
 */
export function LangTag({ lang }: { lang: string }): JSX.Element {
  const name = LANG_NAMES[lang];
  return (
    <>
      <span className="translation-lang" title={name ? `Spoken in ${name}` : "Spoken language"}>
        {lang.toUpperCase()}
      </span>{" "}
    </>
  );
}

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
