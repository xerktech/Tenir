/** Recorded session history & search, with transcripts and retained audio. */

import { history, type Conversation, type CueView, type SegmentView } from "@tenir/client-core";
import { useState } from "react";
import { Linking, Text, View } from "react-native";

import { useHistory } from "../lib/controllers";
import { conversationLabel, errText, msToClock } from "../lib/format";
import { useNotify } from "../lib/notify";
import { audioPlayerAvailable } from "../native/audioPlayer";
import { AudioPlayer } from "../ui/AudioPlayer";
import { CueModal, InlineCue } from "../ui/cue";
import {
  Button,
  ConfirmButton,
  EmptyState,
  Field,
  Heading,
  ListItem,
  Muted,
  Row,
  Screen,
  Spinner,
} from "../ui/components";
import { useTheme } from "../ui/ThemeContext";

// A transcript row is either a spoken segment or a private cue, on one timeline.
type TranscriptItem =
  | { kind: "segment"; at: number; seg: SegmentView }
  | { kind: "cue"; at: number; cue: CueView };

function timeline(conv: Conversation): TranscriptItem[] {
  const items: TranscriptItem[] = [
    ...conv.segments.map((seg) => ({ kind: "segment" as const, at: seg.startMs, seg })),
    ...(conv.cues ?? []).map((cue) => ({ kind: "cue" as const, at: cue.atMs, cue })),
  ];
  return items.sort((a, b) => a.at - b.at || (a.kind === "cue" ? 1 : 0) - (b.kind === "cue" ? 1 : 0));
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
  const [openCue, setOpenCue] = useState<CueView | null>(null);
  const items = timeline(conv);
  return (
    <Screen>
      <Row>
        <Button title="← History" onPress={onBack} />
        <Heading>Session detail</Heading>
      </Row>
      <Muted>{conversationLabel(conv)}</Muted>
      <View>
        {/* An empty transcript block reads as a detail that failed to open — name it. */}
        {conv.segments.length === 0 && (conv.cues?.length ?? 0) === 0 && (
          <Muted>No transcript was recorded for this session.</Muted>
        )}
        {items.map((item) =>
          item.kind === "segment" ? (
            <Text key={item.seg.segmentId} style={{ color: colors.text }}>
              <Text style={{ color: colors.muted }}>[{msToClock(item.seg.startMs)}] </Text>
              {item.seg.text}
            </Text>
          ) : (
            <InlineCue key={item.cue.cueId} title={item.cue.title} onPress={() => setOpenCue(item.cue)} />
          ),
        )}
      </View>
      {openCue && (
        <CueModal title={openCue.title} body={openCue.body} onClose={() => setOpenCue(null)} />
      )}
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
