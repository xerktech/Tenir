/**
 * Live synced-lyrics UI for the mobile app (XERK-184) — parity with the web SPA's
 * `LiveLyricsBand`. When a song is recognized playing, its time-synced lyrics
 * scroll in the same box a cue uses: title "TITLE — ARTIST" over a fixed window
 * of lyric lines, the current one highlighted, advancing as the song plays.
 * Themed via the shared ThemeContext.
 */

import {
  currentLyricIndex,
  type LiveSong,
  type LyricWindow,
  lyricWindow,
} from "@tenir/client-core";
import { useEffect, useState } from "react";
import { StyleSheet, Text, View } from "react-native";

import { useThemedStyles } from "./ThemeContext";
import { mix, radius, space, type Palette } from "./theme";

// How often the lyric scroll recomputes the current line (XERK-184) — sub-second
// so the highlight turns over close to the beat, matching the cue countdown tick.
const LYRIC_SCROLL_TICK_MS = 250;

/**
 * Advance the visible lyric window off the local clock (XERK-184). Recomputes the
 * current line from the song's scroll anchor every tick, so it scrolls smoothly
 * between the server's periodic `song.sync` re-anchors and a backgrounded app
 * resyncs to the truth on return. Re-seeds whenever the song object changes (a
 * new song, or a re-anchored one). Mirrors the web `useLyricScroll`.
 */
function useLyricScroll(song: LiveSong | null): LyricWindow {
  const [win, setWin] = useState<LyricWindow>({ lines: [], currentIndex: -1 });
  useEffect(() => {
    if (!song) {
      setWin({ lines: [], currentIndex: -1 });
      return;
    }
    const tick = () => setWin(lyricWindow(song.lines, currentLyricIndex(song, Date.now())));
    tick();
    const timer = setInterval(tick, LYRIC_SCROLL_TICK_MS);
    return () => clearInterval(timer);
  }, [song]);
  return win;
}

/**
 * The recognized song's synced lyrics in the cue box (XERK-184). A live song owns
 * the box — the caller hides the cue band while it shows. Floats over the
 * transcript's top edge exactly like the cue band, so nothing reflows.
 */
export function LiveLyricsBand({ song }: { song: LiveSong | null }): JSX.Element | null {
  const styles = useThemedStyles(makeStyles);
  const win = useLyricScroll(song);
  if (!song) return null;
  return (
    <View pointerEvents="box-none" style={styles.band}>
      <View style={styles.card}>
        <View style={styles.cardHead}>
          <Text style={styles.cardTitle}>
            {song.title} — {song.artist}
          </Text>
          <Text style={styles.badge} accessibilityElementsHidden>
            ♪
          </Text>
        </View>
        <View style={styles.body}>
          {win.lines.length === 0 ? (
            // No synced lyrics were found: show the title, no scroll.
            <Text style={[styles.line, styles.empty]}>♪ ♪ ♪</Text>
          ) : (
            win.lines.map((ln, i) => (
              <Text
                key={`${ln.atMs}-${i}`}
                numberOfLines={1}
                style={[styles.line, i === win.currentIndex ? styles.current : null]}
              >
                {ln.text || "♪"}
              </Text>
            ))
          )}
        </View>
      </View>
    </View>
  );
}

const makeStyles = (colors: Palette) =>
  StyleSheet.create({
    // Floats over the transcript's top edge instead of displacing it (XERK-107),
    // matching the cue band.
    band: { position: "absolute", top: 0, left: 0, right: 0, zIndex: 2, gap: space.sm },
    card: {
      borderColor: colors.accent,
      borderWidth: 1,
      borderRadius: radius.md,
      backgroundColor: mix(colors.accent, colors.surfaceRaised, 0.14),
      elevation: 4,
      padding: space.md,
      gap: 2,
    },
    cardHead: {
      flexDirection: "row",
      alignItems: "center",
      justifyContent: "space-between",
      gap: space.sm,
    },
    cardTitle: { color: colors.accentStrong, fontWeight: "700", fontSize: 12, flexShrink: 1 },
    badge: { color: colors.accentStrong, fontSize: 12, flexShrink: 0 },
    body: { gap: 1 },
    // Dim by default; the current line lifts to full weight/colour so the eye
    // lands on where the song is now and the rest read as upcoming.
    line: { color: colors.muted, lineHeight: 20 },
    current: { color: colors.accentStrong, fontWeight: "700" },
    empty: { letterSpacing: 4 },
  });
