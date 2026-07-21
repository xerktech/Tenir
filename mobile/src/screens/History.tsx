/** Recorded session history & search, with transcripts and retained audio. */

import { history, type Conversation } from "@tenir/client-core";
import { useState } from "react";
import { Linking, Text, View } from "react-native";

import { useHistory } from "../lib/controllers";
import { conversationLabel, errText, msToClock } from "../lib/format";
import { useNotify } from "../lib/notify";
import { Button, Field, Heading, ListItem, Muted, Row, Screen, Spinner } from "../ui/components";
import { colors } from "../ui/theme";

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
          <Muted>Could not load history: {errText(ctrl.error)}</Muted>
          <Button title="Retry" onPress={() => ctrl.reload()} />
        </>
      )}
      {ctrl.error == null && ctrl.data?.length === 0 && <Muted>No sessions.</Muted>}
      {ctrl.data?.map((c) => (
        <ListItem key={c.id}>
          <Text style={{ color: colors.text }}>{conversationLabel(c)}</Text>
          <Row>
            <Button title="Open" onPress={() => void open(c.id)} />
            <Button title="Delete" kind="danger" onPress={() => void remove(c.id)} />
          </Row>
        </ListItem>
      ))}
    </Screen>
  );
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
  return (
    <Screen>
      <Row>
        <Button title="← History" onPress={onBack} />
        <Heading>Session detail</Heading>
      </Row>
      <Muted>{conversationLabel(conv)}</Muted>
      <View>
        {/* An empty transcript block reads as a detail that failed to open — name it. */}
        {conv.segments.length === 0 && <Muted>No transcript was recorded for this session.</Muted>}
        {conv.segments.map((s) => (
          <Text key={s.segmentId} style={{ color: colors.text }}>
            <Text style={{ color: colors.muted }}>[{msToClock(s.startMs)}] </Text>
            {s.text}
          </Text>
        ))}
      </View>
      {conv.hasAudio && (
        <Button title="Play audio" onPress={() => void Linking.openURL(history.audioUrl(conv.id))} />
      )}
      <Button title="Delete session" kind="danger" onPress={onDelete} />
    </Screen>
  );
}
