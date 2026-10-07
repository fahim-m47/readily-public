import AdvancedControls from "./shell/AdvancedControls";
import AdvancedDiagnostics from "./shell/AdvancedDiagnostics";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { EngineClient, HistoryNarration, Mode } from "./engine/client";
import { useEngine } from "./engine/useEngine";
import CatalogSheet from "./shell/CatalogSheet";
import Composer from "./shell/Composer";
import FirstRunScreen from "./shell/FirstRunScreen";
import HistoryRows from "./shell/HistoryRows";
import NarrationStatus from "./shell/NarrationStatus";
import OpenedNarration from "./shell/OpenedNarration";
import PlayerBar from "./shell/PlayerBar";
import { AudioLines, Download, Plus } from "lucide-react";
import Sidebar, { SidebarToggle } from "./shell/Sidebar";
import ModeMenu from "./shell/ModeMenu";
import StopPrompt from "./shell/StopPrompt";
import UpdatePrompt from "./shell/UpdatePrompt";
import SettingsSheet from "./shell/SettingsSheet";
import ModelMenu from "./shell/ModelMenu";
import VoiceCarousel from "./shell/VoiceCarousel";
import { orbIdentity } from "./shell/orb/identity";
import { fasterVoice } from "./shell/faster";
import { readAlongBlocks, type ReadAlongBlock } from "./shell/readalong";
import { useCatalog } from "./shell/useCatalog";
import { useControls } from "./shell/useControls";
import { useDownloads } from "./shell/useDownloads";
import { useFirstRun } from "./shell/useFirstRun";
import { useHistory } from "./shell/useHistory";
import { usePlayback } from "./shell/usePlayback";
import { useUpdate } from "./shell/useUpdate";
import { useVoice } from "./shell/useVoice";
import { titleOf } from "./shell/history";
import { sourceText } from "./shell/source";
import { describeSelection } from "./shell/voice";
import { describeConnection, describeNarration, isGenerating, isReading, isStoppable } from "./shell/lifecycle";
import "./App.css";

// Enough of a Source to know which Narration the stop prompt means: the
// sidebar row's title, cut to fit a sentence. Cut by code points, never
// between an emoji's surrogate halves.
const firstWords = (title: string) => {
  const text = sourceText(title);
  return text.length <= 48 ? title : `${text.slice(0, 48).trimEnd()}…`;
};

// The title strip's version: the Source's first six words, so the title
// never runs the width of the window. A narrow window cuts it shorter still.
const titleWords = (title: string) => {
  const words = title.split(" ");
  return words.length <= 6 ? title : `${words.slice(0, 6).join(" ")}…`;
};

export default function App({ client }: { client: EngineClient }) {
  const [text, setText] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [mode, setMode] = useState<Mode>("simple");
  // Where the reader asked to go while a Narration was still being
  // generated, waiting on their answer to the prompt.
  const [pending, setPending] = useState<(() => void) | null>(null);
  // A stop the prompt has asked for and the Engine has not answered yet.
  const [stopping, setStopping] = useState(false);
  const engine = useEngine(client);
  const history = useHistory(client, engine);
  const catalog = useCatalog(client, engine);
  const downloads = useDownloads(client, engine, catalog);
  const voice = useVoice(client, engine);
  const controls = useControls(client, voice.selection);
  const playback = usePlayback(client, engine, history.opened);
  const update = useUpdate(client);
  const firstRun = useFirstRun(engine, catalog, downloads);
  // Tauri's own drag-and-drop handler is off so the composer's drop event
  // carries the file, which leaves a file let go anywhere else — the
  // sidebar, the player bar — to the webview, whose default is to open it
  // in place of the app. So every file drag the composer has not already
  // claimed is refused here, at the document. A drag of plain text is left
  // alone and still lands in whatever input it is dropped on.
  useEffect(() => {
    const refuse = (event: DragEvent) => {
      if (event.defaultPrevented || !event.dataTransfer?.types.includes("Files")) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "none";
    };
    document.addEventListener("dragover", refuse);
    document.addEventListener("drop", refuse);
    return () => {
      document.removeEventListener("dragover", refuse);
      document.removeEventListener("drop", refuse);
    };
  }, []);

  // The Engine's snapshot when it is reading the document whose player is
  // visible, otherwise `null`: a stored Narration has no clock of its own.
  const live =
    playback.playerNarration !== null &&
    playback.playerNarration.id === engine.narration?.narrationId
      ? engine.narration
      : null;

  const refusal = engine.actionError
    ? ({ tone: "failed", message: engine.actionError } as const)
    : null;
  const narrationLine = refusal ?? describeNarration(engine.narration);
  // With a player on screen the row under it only *announces* the sentence:
  // the transport already tells the story, Pause for reading and Play for
  // paused or finished. It shows the sentence during the wait before the
  // first words, when the transport is disabled and says nothing. A problem
  // is shown too, but by the player itself, in a line under the transport —
  // a refusal of what the reader just asked, or the failure of the Narration
  // the player is about. A failure of some other Narration, one the reader
  // has left behind for a stored document, is announced and nothing more.
  // While the row is showing the sentence itself, the player shows nothing:
  // one sentence, one place.
  const quiet = playback.playerNarration !== null && live?.phase !== "preparing";
  const trouble = refusal ?? describeNarration(live);
  const problem = quiet && trouble?.tone === "failed" ? trouble.message : null;

  // The last cut is handed back in so a refetch of a Narration being read
  // re-cuts only the Blocks that changed. Kept as state set during render,
  // which React settles before it commits, rather than in a ref, which
  // render may not read.
  const [cut, setCut] = useState<{ narration: HistoryNarration | null; blocks: ReadAlongBlock[] }>({
    narration: null,
    blocks: [],
  });
  if (cut.narration !== playback.playerNarration) {
    setCut({
      narration: playback.playerNarration,
      blocks:
        playback.playerNarration === null
          ? []
          : readAlongBlocks(playback.playerNarration, cut.blocks),
    });
  }
  const { blocks } = cut;
  // The Engine moves the playhead of a Narration it is preparing, playing or
  // has paused. A finished one it will only read again — so under a finished
  // Narration, Play is a replay from the top, and every move of the playhead
  // is a replay first: opened silent, then taken to the target, where the
  // Engine starts it playing. Under any other settled Narration there is
  // nothing to move.
  //
  // One replay at a time, since the Engine refuses to resume a Narration it
  // is already preparing: `replaying` is the one in flight, and a second
  // ask — a move, or Play — only changes what happens once it is admitted.
  // That happens on the Engine's own snapshot, not the replay's reply: a
  // seek moves whatever is active, and between the two another Narration
  // can be admitted — a row opened, the composer sent. A replay that fails,
  // is overtaken, or loses the connection takes its move with it.
  const finishedId = live?.phase === "finished" ? live.narrationId : null;
  const replaying = useRef<{ id: string; move: (() => void) | null } | null>(null);
  useEffect(() => {
    const waiting = replaying.current;
    if (waiting === null) return;
    const snapshot = engine.narration;
    if (snapshot === null || snapshot.narrationId !== waiting.id || snapshot.phase === "failed") {
      replaying.current = null;
      return;
    }
    if (!isStoppable(snapshot)) return;
    replaying.current = null;
    waiting.move?.();
  }, [engine.narration]);
  const { replay: engineReplay } = engine;
  const replay = useCallback(
    (id: string, move: (() => void) | null) => {
      if (replaying.current !== null) {
        replaying.current.move = move;
        return;
      }
      const asked = { id, move };
      replaying.current = asked;
      void engineReplay(id, { paused: move !== null }).then((taken) => {
        if (!taken && replaying.current === asked) replaying.current = null;
      });
    },
    [engineReplay],
  );
  const afterReplay = useCallback(
    (move: (target: number) => void) =>
      finishedId === null ? null : (target: number) => replay(finishedId, () => move(target)),
    [finishedId, replay],
  );
  const stoppable = isStoppable(live);
  // One identity per state, so the memoized Blocks under a finished
  // Narration do not all re-render on every snapshot. `seekTime` goes to
  // the player bar, which is not memoized, so it needs none.
  const seek = useMemo(
    () => (stoppable ? engine.seek : afterReplay(engine.seek)),
    [stoppable, engine.seek, afterReplay],
  );
  const seekTime = stoppable ? engine.seekTime : afterReplay(engine.seekTime);
  // Cancel is a stop that also leaves the document: the reader changed
  // their mind about the text or the Voice, so the composer comes back with
  // the text still in it. A Narration that kept any audio waits in History
  // to be resumed; one that kept none is gone, and there is nothing to show.
  const cancel = () =>
    engine.stop().then((stopped) => {
      if (stopped) playback.dismiss();
      return stopped;
    });
  // Play on a finished Narration is a replay out loud from the top. If a
  // silent one is already in flight, the Engine only knows `paused` for
  // Play, so the move becomes a seek to the top, which the Engine starts
  // reading from.
  const play =
    finishedId === null
      ? engine.play
      : () => replay(finishedId, replaying.current === null ? null : () => engine.seekTime(0));

  const faster = fasterVoice(
    engine.narration,
    playback.narration,
    catalog.entries,
    catalog.statusOf,
    catalog.defaultFastModelId,
  );
  const fasterOffer =
    faster === null
      ? null
      : {
          name: faster.name,
          onTake: () => {
            voice.select(faster.selection);
            engine.narrate(faster.source, faster.selection, mode);
          },
        };

  const voiceModelInstalled = catalog.voiceModelInstalled(
    voice.selection?.modelId ?? null,
  );

  const canNarrate =
    engine.connection.state === "ready" &&
    !voice.readFailed &&
    voiceModelInstalled !== false;

  const composerNotice =
    voiceModelInstalled === false
      ? "Readily needs the chosen voice model on disk to read with. Pick it from the model menu to download it."
      : voice.notice;

  const chosenModel =
    catalog.entries?.find((entry) => entry.id === voice.selection?.modelId) ?? null;
  const composing = text.length > 0;

  const leave = () => {
    history.close();
    playback.dismiss();
  };

  const open = (narrationId: string) => {
    playback.reveal(narrationId);
    history.open(narrationId);
  };

  // Moving on — another row, a New Narration, narrating something else —
  // cancels a Narration still being generated, so the reader is asked
  // first. `next` runs at once when nothing is generating, otherwise only
  // after the Engine has confirmed the stop.
  const afterGenerating = (next: () => void) => {
    if (isGenerating(engine.narration)) setPending(() => next);
    else next();
  };

  // The question is over once the Engine stops generating, whether or not it
  // is still reading aloud. A missing snapshot says nothing either way.
  const moot = !stopping && engine.narration !== null && !isGenerating(engine.narration);
  const overlaid = browsing || settingsOpen || pending !== null;
  const playingEntry = history.entries.find(
    (entry) => entry.id === engine.narration?.narrationId,
  );

  const rows =
    history.entries.length > 0 || history.notice ? (
      <HistoryRows
        entries={history.entries}
        activeId={engine.narration?.narrationId ?? null}
        notice={history.notice}
        onOpen={(narrationId) => {
          if (engine.narration?.narrationId === narrationId) open(narrationId);
          else afterGenerating(() => open(narrationId));
        }}
        onDelete={history.remove}
        onExport={history.exportAudio}
      />
    ) : undefined;

  if (firstRun.step !== null) {
    return (
      <FirstRunScreen step={firstRun.step} onRetry={firstRun.onRetry} terms={firstRun.terms} />
    );
  }

  return (
    <div className={`shell${sidebarOpen ? "" : " shell--bare"}`}>
      <Sidebar
        modeControl={<ModeMenu mode={mode} disabled={isReading(engine.narration)} onChange={setMode} />}
        history={rows}
        status={describeConnection(engine.connection)}
        open={sidebarOpen}
        onNewNarration={() => afterGenerating(leave)}
        onSettings={() => setSettingsOpen(true)}
        onHide={() => setSidebarOpen(false)}
      />

      <main className="main" aria-label="Readily">
        {/* The title bar's right half: the opened Narration's title — its
            Source's first words — and what can be done with it, level with
            the traffic lights. With the sidebar away the lights sit in this
            strip's padding instead, and New comes along so the composer stays
            one click away. */}
        <div
          className={`bar main__bar${sidebarOpen ? "" : " bar--lights"}`}
          data-tauri-drag-region="deep"
        >
          {!sidebarOpen && (
            <>
              <SidebarToggle autoFocus open={false} onClick={() => setSidebarOpen(true)} />
              <button
                aria-label="New Narration"
                className="bar__button"
                onClick={() => afterGenerating(leave)}
                type="button"
              >
                <Plus aria-hidden="true" size={18} strokeWidth={1.75} />
              </button>
            </>
          )}
          {playback.narration && (
            <h1 className="main__title">
              <AudioLines aria-hidden="true" size={15} strokeWidth={1.75} />
              <span className="main__title-text">{titleWords(titleOf(playback.narration))}</span>
            </h1>
          )}
          {playback.playerNarration && (
            <button className="main__export" onClick={playback.exportAudio} type="button">
              <Download aria-hidden="true" size={14} />
              Export
            </button>
          )}
        </div>
        {playback.narration ? (
          <div className="main__reading">
            <OpenedNarration
              blocks={blocks}
              positionSec={live?.positionSec ?? null}
              onSeek={seek}
            />
          </div>
        ) : (
          <div className={`main__compose${composing ? " main__compose--composing" : ""}`}>
            <h1 className="main__greeting">What should I read?</h1>

            {chosenModel !== null && voice.selection !== null && (
              <VoiceCarousel
                modelId={chosenModel.id}
                voices={chosenModel.voices}
                voiceId={voice.selection.voiceId}
                orb={(voiceId) => orbIdentity(catalog.entries, { modelId: chosenModel.id, voiceId })}
                onSelect={(voiceId) => voice.select({ modelId: chosenModel.id, voiceId })}
                compact={composing}
                silenced={overlaid}
              />
            )}

            <Composer
              text={text}
              onTextChange={setText}
              canNarrate={canNarrate}
              onNarrate={() =>
                afterGenerating(() => engine.narrate(text, voice.selection ?? undefined, mode))
              }
              fetchPage={client.fetchPage}
              notice={composerNotice}
              model={
                <ModelMenu
                  entries={catalog.entries}
                  statusOf={catalog.statusOf}
                  downloads={downloads}
                  selection={voice.selection}
                  onSelect={voice.select}
                  onBrowse={() => setBrowsing(true)}
                />
              }
            />
          </div>
        )}

        {playback.playerNarration && (
          <PlayerBar
            narration={live}
            totalSec={
              playback.playerNarration.totalDurationSec ?? live?.totalSec ?? 0
            }
            notice={playback.notice}
            problem={problem}
            voice={describeSelection(catalog.entries, {
              modelId: playback.playerNarration.modelId,
              voiceId: playback.playerNarration.voiceId,
            })}
            orb={orbIdentity(catalog.entries, playback.playerNarration)}
            faster={fasterOffer}
            onPause={engine.pause}
            onPlay={play}
            onSeekTime={seekTime}
            speed={engine.narration?.speed ?? 1}
            onSpeed={engine.setSpeed}
            onCancel={isReading(live) ? cancel : null}
          />
        )}

        {mode === "advanced" && <div className="advanced-panel">
          <AdvancedDiagnostics key={engine.narration?.narrationId ?? "idle"} client={client} narration={engine.narration}
            supportModels={(catalog.entries ?? []).flatMap((entry) => entry.wordTimingModels ?? [])}
            onTakeSelected={playback.reread} />
          {chosenModel?.parameters && voice.selection && <AdvancedControls
            key={`${voice.selection.modelId}/${voice.selection.voiceId}`}
            name={describeSelection(catalog.entries, voice.selection) ?? chosenModel.name} parameters={chosenModel.parameters}
            wordTimingModels={chosenModel.wordTimingModels ?? []}
            settings={controls.settings} failure={controls.failure} save={controls.save} />}
        </div>}

        <NarrationStatus status={narrationLine} quiet={quiet} />
      </main>

      {browsing && (
        <CatalogSheet
          catalog={catalog}
          downloads={downloads}
          onClose={() => setBrowsing(false)}
        />
      )}

      {pending !== null && (
        <StopPrompt
          generating={
            playingEntry === undefined
              ? "another Narration"
              : firstWords(titleOf(playingEntry))
          }
          moot={moot}
          onMoot={pending}
          onStop={() => {
            setStopping(true);
            return engine.stop().then((stopped) => {
              if (stopped) pending();
            });
          }}
          // `stopping` clears here, not when the stop settles, so the prompt is
          // never unmounted from under an open dialog and focus stays where
          // the reader asked from.
          onKeep={() => {
            setPending(null);
            setStopping(false);
          }}
        />
      )}

      {settingsOpen && (
        <SettingsSheet
          client={client}
          voice={voice}
          onClose={() => setSettingsOpen(false)}
        />
      )}

      {/* Last, so a newer Readily is the thing on top when the launch check
        * finds one. Nothing installs until the reader says so, and the first-run
        * screen returns above this, so a reader still waiting on the Engine is
        * never asked about a version instead. */}
      {update.offer !== null && (
        <UpdatePrompt
          offer={update.offer}
          onInstall={update.install}
          onDownload={update.openDownloadPage}
          onLater={update.dismiss}
        />
      )}
    </div>
  );
}
