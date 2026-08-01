/** Recorded session history & search, with transcripts and retained audio. */

import {
  history,
  type Conversation,
  type CueView,
  type SegmentView,
  type SongView,
} from "@tenir/client-core";
import { useState } from "react";
import { Linking, Switch, Text, View } from "react-native";

import { useHistory } from "../lib/controllers";
import { conversationLabel, errText, msToClock } from "../lib/format";
import { useNotify } from "../lib/notify";
import { audioPlayerAvailable } from "../native/audioPlayer";
import { AudioPlayer } from "../ui/AudioPlayer";
import { CueDisclosure } from "../ui/cue";
import {
  Button,
  ConfirmButton,
  EmptyState,
  Field,
  Heading,
  ListItem,
  LangTagText,
  Muted,
  Row,
  Screen,
  Spinner,
  TranslationText,
} from "../ui/components";
import { useTheme } from "../ui/ThemeContext";

// A transcript row is a spoken segment, a private cue, or a recognized song, on
// one timeline (XERK-81, XERK-184).
type TranscriptItem =
  | { kind: "segment"; at: number; seg: SegmentView }
  | { kind: "cue"; at: number; cue: CueView }
  | { kind: "song"; at: number; song: SongView };

function timeline(conv: Conversation): TranscriptItem[] {
  const items: TranscriptItem[] = [
    ...conv.segments.map((seg) => ({ kind: "segment" as const, at: seg.startMs, seg })),
    ...(conv.cues ?? []).map((cue) => ({ kind: "cue" as const, at: cue.atMs, cue })),
    ...(conv.songs ?? []).map((song) => ({ kind: "song" as const, at: song.atMs, song })),
  ];
  return items.sort(
    (a, b) => a.at - b.at || (a.kind === "segment" ? 0 : 1) - (b.kind === "segment" ? 0 : 1),
  );
}

// Consecutive spoken turns are grouped into a run so they can render inside one
// selectable <Text> — RN only extends a text selection within a single <Text>
// tree, so a run is the largest span a drag-select can cover. A cue or song is a
// separate tappable/marker view, so it ends the current run.
type Run =
  | { kind: "segments"; segs: SegmentView[] }
  | { kind: "cue"; cue: CueView }
  | { kind: "song"; song: SongView };

function runs(items: TranscriptItem[]): Run[] {
  const out: Run[] = [];
  for (const item of items) {
    if (item.kind === "cue") {
      out.push({ kind: "cue", cue: item.cue });
    } else if (item.kind === "song") {
      out.push({ kind: "song", song: item.song });
    } else {
      const last = out[out.length - 1];
      if (last && last.kind === "segments") last.segs.push(item.seg);
      else out.push({ kind: "segments", segs: [item.seg] });
    }
  }
  return out;
}

export function HistoryScreen(): JSX.Element {
  const notify = useNotify();
  const ctrl = useHistory();
  const [query, setQuery] = useState(ctrl.search);
  const [selected, setSelected] = useState<Conversation | null>(null);

  const open = (id: string) =>
    ctrl
      .open(id)
      .then(setSelected)
      .catch((e) => notify(errText(e), "err"));

  const remove = (id: string) =>
    ctrl
      .remove(id)
      .then(() => {
        if (selected?.id === id) setSelected(null);
      })
      .catch((e) => notify(errText(e), "err"));

  // Opening a session shows its transcript on its own screen, replacing the list.
  // It used to render inline below the list, where it read as the tap doing
  // nothing (XERK-65).
  if (selected) {
    return (
      <Detail conv={selected} onDelete={() => void remove(selected.id)} onBack={() => setSelected(null)} />
    );
  }

  return (
    <Screen>
      <Heading>History &amp; search</Heading>
      <Row>
        <Field placeholder="Search transcripts…" value={query} onChangeText={setQuery} />
        <Button title="Search" onPress={() => ctrl.setSearch(query)} />
      </Row>

      {ctrl.loading && <Spinner />}
      {/* A failed listing used to render as an empty screen, indistinguishable from
          having recorded nothing (XERK-58). Say so, and offer a retry. */}
      {!ctrl.loading && ctrl.error != null && (
        <>
          <EmptyState title="Could not load history" hint={errText(ctrl.error)} />
          <Button title="Retry" onPress={() => ctrl.reload()} />
        </>
      )}
      {ctrl.error == null && ctrl.data?.length === 0 && (
        <EmptyState title="No conversations yet" hint="Captured conversations will appear here." />
      )}
      {ctrl.data?.map((c) => (
        <ListItem key={c.id}>
          <ConversationRow label={conversationLabel(c)} />
          <Row>
            <Button title="Open" onPress={() => void open(c.id)} />
            {/* Destructive actions arm on the first press and commit on the
                second (Turma's two-step pattern) — no confirm dialog. */}
            <ConfirmButton
              title="Delete"
              confirmTitle="Confirm delete"
              onConfirm={() => void remove(c.id)}
            />
          </Row>
        </ListItem>
      ))}
    </Screen>
  );
}

function ConversationRow({ label }: { label: string }): JSX.Element {
  const { colors } = useTheme();
  return <Text style={{ color: colors.text }}>{label}</Text>;
}

function Detail({
  conv,
  onDelete,
  onBack,
}: {
  conv: Conversation;
  onDelete: () => void;
  onBack: () => void;
}): JSX.Element {
  const { colors } = useTheme();
  // Cues default to shown every time a conversation is opened (not persisted) —
  // this component remounts per open, so `useState(true)` resets it (XERK-104).
  const [showCues, setShowCues] = useState(true);
  const hasCues = (conv.cues?.length ?? 0) > 0;
  // With cues hidden there are no cue dropdowns breaking the timeline, so every
  // turn collapses into one run / one selectable <Text> — the whole conversation
  // is then draggable-selectable end to end (RN only selects within one <Text>).
  const items = timeline(conv).filter((it) => it.kind !== "cue" || showCues);
  return (
    <Screen>
      <Row>
        <Button title="← History" onPress={onBack} />
        <Heading>Session detail</Heading>
      </Row>
      <Muted>{conversationLabel(conv)}</Muted>
      {/* Toggle cues off to make the whole transcript one selectable block; only
          shown when the conversation actually has cues to toggle. */}
      {hasCues && (
        <Row>
          <Muted>Show cues</Muted>
          <View style={{ flexGrow: 1 }} />
          <Switch
            accessibilityLabel="Show cues"
            value={showCues}
            onValueChange={setShowCues}
            trackColor={{ true: colors.accent, false: colors.borderStrong }}
            thumbColor={colors.surface}
          />
        </Row>
      )}
      <View>
        {/* An empty transcript block reads as a detail that failed to open — name it. */}
        {conv.segments.length === 0 &&
          (conv.cues?.length ?? 0) === 0 &&
          (conv.songs?.length ?? 0) === 0 && (
            <Muted>No transcript was recorded for this session.</Muted>
          )}
        {/* Each run of consecutive turns is ONE selectable <Text>, so a
            selection can be dragged across every turn in it and copied
            (XERK-104). RN only extends a selection within a single <Text> tree;
            a cue dropdown is a separate view, so it ends a run — hide cues (the
            toggle above) to drag-select the whole conversation at once. */}
        {runs(items).map((run, i) =>
          run.kind === "cue" ? (
            <CueDisclosure
              key={run.cue.cueId}
              title={run.cue.title}
              body={run.cue.body}
              source={run.cue.source}
            />
          ) : run.kind === "song" ? (
            // A song recognized playing during the session (XERK-184), inline at
            // the point it played — the mobile parity of the web history marker.
            <Text key={run.song.songId} style={{ color: colors.muted, lineHeight: 22 }}>
              <Text style={{ color: colors.accentStrong }}>♪ </Text>
              {run.song.artist} — {run.song.title}
            </Text>
          ) : (
            <Text key={`run-${i}`} selectable style={{ color: colors.text, lineHeight: 22 }}>
              {run.segs.map((seg, j) => (
                <Text key={seg.segmentId}>
                  {j > 0 ? "\n" : null}
                  <Text style={{ color: colors.muted }}>[{msToClock(seg.startMs)}] </Text>
                  {/* Source-language chip on a translated turn's original
                      text (XERK-160) — the counterpart of the "EN" tag. */}
                  {seg.translation && seg.lang ? <LangTagText lang={seg.lang} /> : null}
                  {seg.text}
                  {/* Stored English translation of a non-English turn (XERK-160),
                      turn-by-turn inside the same selectable run. */}
                  {seg.translation ? <TranslationText text={seg.translation} /> : null}
                </Text>
              ))}
            </Text>
          ),
        )}
      </View>
      {/* Retained audio plays in-app with a seek bar (XERK-67). Where the native
          player isn't available (iOS), fall back to opening it in the browser. */}
      {conv.hasAudio &&
        (audioPlayerAvailable ? (
          <AudioPlayer url={history.audioUrl(conv.id)} />
        ) : (
          <Button title="Play audio" onPress={() => void Linking.openURL(history.audioUrl(conv.id))} />
        ))}
      <ConfirmButton title="Delete session" confirmTitle="Confirm delete" onConfirm={onDelete} />
    </Screen>
  );
}
