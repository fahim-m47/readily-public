import { expect, test, vi } from "vitest";
import type { Diagnostics } from "./advanced";
import {
  createEngineClient,
  type Connection,
  type NarrationState,
} from "./client";

const CONFIG = { port: 51234, token: "secret-token" };

const QUIET: Diagnostics = {
  audioSecondsPerSecond: null,
  readySecondsAhead: 0,
  preparingBlock: null,
  playingBlock: null,
  retries: 0,
  cutoffs: 0,
  ringStarvations: 0,
  deviceUnderflows: 0,
};

const streamResponse = (...chunks: string[]) =>
  new Response(
    new ReadableStream({
      start(controller) {
        const encoder = new TextEncoder();
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );

test("watch reconnects through supervisor startup and parses chunked SSE", async () => {
  const statuses = [{ state: "starting", attempt: 1 }, { state: "ready", port: 51234 }];
  const invoke = vi.fn(async (command: string) => {
    if (command === "engine_status") return statuses.shift();
    if (command === "engine_config") return CONFIG;
    throw new Error(`unexpected command ${command}`);
  });
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    void input;
    void init;
    return streamResponse(
      "retry: 1000\n\nevent: narration\nid: 1\nda",
      'ta: {"version":1,"phase":"idle","narrationId":null,',
      '"modelId":"kokoro:82m","voiceId":"af_heart","positionSec":0,',
      `"totalSec":0,"speed":1,"generationBehind":false,"diagnostics":${JSON.stringify(QUIET)},"level":0,"lateCallbacks":0,"error":null}\n\n`,
    );
  });
  const controller = new AbortController();
  const connections: string[] = [];
  const snapshots: NarrationState[] = [];
  const client = createEngineClient({
    invoke,
    fetch: fetcher,
    delay: async () => {},
  });

  await client.watch(
    {
      onConnection: (connection) => connections.push(connection.state),
      onNarration: (state) => {
        snapshots.push(state);
        controller.abort();
      },
    },
    controller.signal,
  );

  expect(connections).toEqual(["starting", "ready"]);
  expect(snapshots).toHaveLength(1);
  expect(snapshots[0].phase).toBe("idle");
  expect(fetcher).toHaveBeenCalledWith(
    "http://127.0.0.1:51234/v1/events",
    expect.objectContaining({
      headers: { Authorization: "Bearer secret-token" },
      signal: controller.signal,
    }),
  );
  expect(fetcher.mock.calls[0][0]).not.toContain("secret-token");
});

test("seek sends a Source coordinate, leaving timeline calculation to the Engine", async () => {
  const controller = new AbortController();
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    void init;
    if (String(input).endsWith("/v1/events")) {
      controller.abort();
      return streamResponse();
    }
    return new Response(JSON.stringify({ version: 1, seeked: true }));
  });
  const client = createEngineClient({
    invoke: async (command) => command === "engine_status"
      ? { state: "ready", port: CONFIG.port } : CONFIG,
    fetch: fetcher,
  });
  await client.watch({}, controller.signal);
  await client.seekTime(24.5);
  expect(fetcher).toHaveBeenLastCalledWith(
    `http://127.0.0.1:${CONFIG.port}/v1/audio/seek`,
    expect.objectContaining({ method: "POST", body: JSON.stringify({ positionSec: 24.5 }) }),
  );
  await client.seek(9);
  expect(fetcher).toHaveBeenCalledWith(
    `http://127.0.0.1:${CONFIG.port}/v1/audio/seek`,
    expect.objectContaining({ method: "POST", body: JSON.stringify({ sourceOffset: 9 }) }),
  );
});

test("playback speed is a live setting, not a new speech request", async () => {
  const controller = new AbortController();
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith("/v1/events")) {
      controller.abort();
      return streamResponse();
    }
    void init;
    return new Response(JSON.stringify({ version: 1, speed: 2 }));
  });
  const client = createEngineClient({
    invoke: async (command) => command === "engine_status"
      ? { state: "ready", port: CONFIG.port } : CONFIG,
    fetch: fetcher,
  });
  await client.watch({}, controller.signal);
  await client.setSpeed(2);
  expect(fetcher).toHaveBeenCalledWith(
    `http://127.0.0.1:${CONFIG.port}/v1/settings/playback`,
    expect.objectContaining({ method: "PATCH", body: JSON.stringify({ speed: 2 }) }),
  );
});

test("Narrate sends only the text and lets the Engine resolve the defaults", async () => {
  const invoke = vi.fn(async (command: string) =>
    command === "engine_status" ? { state: "ready", port: 51234 } : CONFIG,
  );
  const controller = new AbortController();
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    void init;
    if (String(input).endsWith("/v1/events")) {
      controller.abort();
      return streamResponse();
    }
    return new Response(
      JSON.stringify({ version: 1, narrationId: "n1", status: "accepted" }),
      { status: 202, headers: { "content-type": "application/json" } },
    );
  });
  const client = createEngineClient({ invoke, fetch: fetcher });
  await client.watch({}, controller.signal);

  await client.narrate("Keep this on my Mac.", undefined, "advanced");

  const [, request] = fetcher.mock.calls.find(([input]) =>
    String(input).endsWith("/v1/audio/speech"),
  )!;
  expect(request?.method).toBe("POST");
  expect(request?.headers).toEqual({
    Authorization: "Bearer secret-token",
    "Content-Type": "application/json",
  });
  expect(JSON.parse(String(request?.body))).toEqual({
    input: "Keep this on my Mac.",
    mode: "advanced",
  });
});

test("Stop uses the same launch config and frozen route", async () => {
  const invoke = vi.fn(async (command: string) =>
    command === "engine_status" ? { state: "ready", port: 51234 } : CONFIG,
  );
  const controller = new AbortController();
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    void init;
    if (String(input).endsWith("/v1/events")) {
      controller.abort();
      return streamResponse();
    }
    return new Response(JSON.stringify({ version: 1, stopped: true }), {
      status: 200,
    });
  });
  const client = createEngineClient({ invoke, fetch: fetcher });
  await client.watch({}, controller.signal);

  await client.stop();

  expect(fetcher).toHaveBeenCalledWith(
    "http://127.0.0.1:51234/v1/audio/stop",
    expect.objectContaining({
      method: "POST",
      headers: { Authorization: "Bearer secret-token" },
      signal: expect.any(AbortSignal),
    }),
  );
});

test("a stop the Engine never answers fails with a sentence, not a hang", async () => {
  const invoke = vi.fn(async (command: string) =>
    command === "engine_status" ? { state: "ready", port: 51234 } : CONFIG,
  );
  const controller = new AbortController();
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith("/v1/events")) {
      controller.abort();
      return streamResponse();
    }
    await new Promise<never>((_, reject) => {
      init?.signal?.addEventListener("abort", () => reject(init.signal?.reason));
    });
    throw new Error("unreachable");
  });
  const client = createEngineClient({ invoke, fetch: fetcher });
  await client.watch({}, controller.signal);

  vi.useFakeTimers();
  try {
    const stopped = expect(client.stop()).rejects.toThrow("The Engine did not answer.");
    await vi.advanceTimersByTimeAsync(10_000);
    await stopped;
  } finally {
    vi.useRealTimers();
  }
});

test("a stream that fails mid-frame is cancelled, not left holding the connection", async () => {
  const stream = { cancelled: false };
  const response = new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            'event: narration\ndata: {"version":2,"phase":"idle"}\n\n',
          ),
        );
        // Never closed: a live SSE connection with more to come.
      },
      cancel() {
        stream.cancelled = true;
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );
  const invoke = vi.fn(async (command: string) =>
    command === "engine_status" ? { state: "ready", port: 51234 } : CONFIG,
  );
  const controller = new AbortController();
  const client = createEngineClient({
    invoke,
    fetch: async () => response,
    delay: async () => {},
  });
  const connections: Connection[] = [];

  await client.watch(
    {
      onConnection: (connection) => {
        connections.push(connection);
        if (connection.state === "failed") controller.abort();
      },
    },
    controller.signal,
  );

  expect(connections.at(-1)).toEqual({
    state: "failed",
    message: "The Engine sent an unsupported event version.",
  });
  expect(stream.cancelled).toBe(true);
});

test.each<[string, Record<string, unknown>]>([
  ["no speed", { speed: undefined }],
  ["a speed that is not a number", { speed: "fast" }],
  ["a speed that is not finite", { speed: Infinity }],
  ["no diagnostics", { diagnostics: undefined }],
  ["diagnostics it cannot read", { diagnostics: { ...QUIET, readySecondsAhead: null } }],
])(
  "a narration stream rejects a snapshot with %s",
  async (_label, broken) => {
    const snapshot = {
      version: 1,
      phase: "idle",
      narrationId: null,
      modelId: "kokoro:82m",
      voiceId: "af_heart",
      positionSec: 0,
      totalSec: 0,
      speed: 1,
      generationBehind: false,
      diagnostics: QUIET,
      level: 0,
      lateCallbacks: 0,
      error: null,
      ...broken,
    };
    const response = streamResponse(
      `event: narration\ndata: ${JSON.stringify(snapshot)}\n\n`,
    );
    const controller = new AbortController();
    const client = createEngineClient({
      invoke: async (command) => command === "engine_status"
        ? { state: "ready", port: CONFIG.port } : CONFIG,
      fetch: async () => response,
      delay: async () => {},
    });
    const connections: Connection[] = [];
    const snapshots: NarrationState[] = [];

    await client.watch(
      {
        onConnection: (connection) => {
          connections.push(connection);
          if (connection.state === "failed") controller.abort();
        },
        onNarration: (state) => snapshots.push(state),
      },
      controller.signal,
    );

    expect(snapshots).toEqual([]);
    expect(connections.at(-1)).toEqual({
      state: "failed",
      message: "The Engine sent an unsupported event version.",
    });
  },
);

test("the watch loop's delays leave no abort listeners behind", async () => {
  vi.useFakeTimers();
  try {
    const controller = new AbortController();
    const added = vi.spyOn(controller.signal, "addEventListener");
    const removed = vi.spyOn(controller.signal, "removeEventListener");
    const client = createEngineClient({
      invoke: async () => ({ state: "starting", attempt: 1 }),
      fetch: async () => {
        throw new Error("a provisioning Engine is never fetched");
      },
    });

    const watching = client.watch({}, controller.signal);
    for (let tick = 0; tick < 5; tick += 1) {
      await vi.advanceTimersByTimeAsync(250);
    }
    controller.abort();
    await watching;

    expect(added.mock.calls.length).toBeGreaterThanOrEqual(5);
    expect(removed.mock.calls.length).toBe(added.mock.calls.length);
  } finally {
    vi.useRealTimers();
  }
});

test("versioned Engine errors become useful client errors", async () => {
  const invoke = vi.fn(async (command: string) =>
    command === "engine_status" ? { state: "ready", port: 51234 } : CONFIG,
  );
  const controller = new AbortController();
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    void init;
    if (String(input).endsWith("/v1/events")) {
      controller.abort();
      return streamResponse();
    }
    return new Response(
      JSON.stringify({
        error: {
          version: 1,
          code: "invalid_request",
          message: "The request body does not match the v1 speech contract.",
        },
      }),
      { status: 422, headers: { "content-type": "application/json" } },
    );
  });
  const client = createEngineClient({ invoke, fetch: fetcher });
  await client.watch({}, controller.signal);

  await expect(client.narrate("bad", undefined, "advanced")).rejects.toThrow(
    "The request body does not match the v1 speech contract.",
  );
});

const readyClient = (
  handle: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>,
  save?: Parameters<typeof createEngineClient>[0]["save"],
) => {
  const invoke = vi.fn(async (command: string) =>
    command === "engine_status" ? { state: "ready", port: 51234 } : CONFIG,
  );
  const controller = new AbortController();
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith("/v1/events")) {
      controller.abort();
      return streamResponse();
    }
    return handle(input, init);
  });
  const client = createEngineClient({ invoke, fetch: fetcher, save });
  return { client, fetcher, ready: client.watch({}, controller.signal) };
};

test("export asks the shell for a destination and hands the Engine the path", async () => {
  const save = vi.fn(async () => "/Users/reader/Desktop/An article.wav");
  const { client, fetcher, ready } = readyClient(
    async () => new Response(null, { status: 202 }),
    save,
  );
  await ready;

  await expect(client.exportNarration("n 1", { format: "wav", defaultName: "An article" }))
    .resolves.toBe(true);

  expect(save).toHaveBeenCalledWith({
    defaultPath: "An article.wav",
    filters: [{ name: "WAV", extensions: ["wav"] }],
  });
  const [url, init] = fetcher.mock.calls[1];
  expect(url).toBe("http://127.0.0.1:51234/v1/history/n%201/export");
  expect(JSON.parse(String(init?.body))).toEqual({
    destination: "/Users/reader/Desktop/An article.wav",
    format: "wav",
  });
});

test("a dismissed save panel exports nothing", async () => {
  const { client, fetcher, ready } = readyClient(
    async () => {
      throw new Error("a cancelled Export never reaches the Engine");
    },
    async () => null,
  );
  await ready;

  await expect(client.exportNarration("n-1")).resolves.toBe(false);
  expect(fetcher).toHaveBeenCalledTimes(1);
});

test("export defaults to M4A", async () => {
  const save = vi.fn(async () => "/Users/reader/Desktop/read.m4a");
  const { client, fetcher, ready } = readyClient(
    async () => new Response(null, { status: 202 }),
    save,
  );
  await ready;

  await client.exportNarration("n-1");

  expect(save).toHaveBeenCalledWith({
    defaultPath: undefined,
    filters: [{ name: "M4A", extensions: ["m4a"] }],
  });
  expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body)).format).toBe("m4a");
});

test("a refused destination surfaces the Engine's own sentence", async () => {
  const { client, ready } = readyClient(
    async () =>
      new Response(
        JSON.stringify({
          error: {
            version: 1,
            code: "invalid_destination",
            message: "The Engine will not write an Export to that location.",
          },
        }),
        { status: 422, headers: { "content-type": "application/json" } },
      ),
    async () => "/Users/reader/Library/Application Support/Readily/readily.db",
  );
  await ready;

  await expect(client.exportNarration("n-1")).rejects.toThrow(
    "The Engine will not write an Export to that location.",
  );
});

test("the supervisor's pre-ready states arrive as three distinguishable waits", async () => {
  const statuses = [
    { state: "provisioning", note: null },
    { state: "starting", attempt: 1 },
    { state: "restarting", attempt: 2, retryInMs: 1000 },
    { state: "failed", reason: "The Engine would not start." },
  ];
  const client = createEngineClient({
    invoke: async () => statuses.shift(),
    fetch: async () => {
      throw new Error("a pre-ready Engine is never fetched");
    },
    delay: async () => {},
  });
  const seen: Connection[] = [];
  const controller = new AbortController();

  await client.watch(
    {
      onConnection: (connection) => {
        seen.push(connection);
        // The loop no longer ends on a failure, so the test is what stops it.
        if (connection.state === "failed") controller.abort();
      },
    },
    controller.signal,
  );

  expect(seen).toEqual([
    { state: "starting", detail: "provisioning", note: null },
    { state: "starting", detail: "launching" },
    { state: "starting", detail: "restarting" },
    { state: "failed", message: "The Engine would not start." },
  ]);
});

test("a failed Engine keeps being watched, so a retry is noticed", async () => {
  const statuses = [
    { state: "failed", reason: "The Engine would not start." },
    { state: "failed", reason: "The Engine would not start." },
    { state: "provisioning", note: null },
    { state: "starting", attempt: 1 },
  ];
  const client = createEngineClient({
    invoke: async () => statuses.shift(),
    fetch: async () => {
      throw new Error("a pre-ready Engine is never fetched");
    },
    delay: async () => {},
  });
  const seen: Connection[] = [];
  const controller = new AbortController();

  await client.watch(
    {
      onConnection: (connection) => {
        seen.push(connection);
        if (connection.state === "starting" && connection.detail === "launching") {
          controller.abort();
        }
      },
    },
    controller.signal,
  );

  expect(seen).toEqual([
    { state: "failed", message: "The Engine would not start." },
    { state: "starting", detail: "provisioning", note: null },
    { state: "starting", detail: "launching" },
  ]);
});

test("asking for a retry is one invoke and nothing else", async () => {
  const invoke = vi.fn(async () => null);
  const client = createEngineClient({ invoke });

  await client.retryEngine();

  expect(invoke).toHaveBeenCalledWith("engine_retry");
});

const HISTORY_ENTRY = {
  id: "n-1",
  sourcePreview: "Read locally.",
  modelId: "kokoro:82m",
  voiceId: "af_heart",
  speed: 1,
  status: "interrupted",
  createdAt: "2026-08-26T00:00:00+00:00",
  updatedAt: "2026-08-26T00:00:00+00:00",
  lastPlayedAt: "2026-08-26T00:00:00+00:00",
  playheadSec: 0.5,
  totalDurationSec: null,
  audioPresent: false,
  hasGaps: true,
};

const json = (body: object, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

test("History comes back in the Engine's own order, over the Engine's own token", async () => {
  const { client, fetcher, ready } = readyClient(async () =>
    json({ version: 1, history: [HISTORY_ENTRY, { ...HISTORY_ENTRY, id: "n-0" }] }),
  );
  await ready;

  const history = await client.listHistory();

  expect(history.map((entry) => entry.id)).toEqual(["n-1", "n-0"]);
  const [url, init] = fetcher.mock.calls[1];
  expect(url).toBe("http://127.0.0.1:51234/v1/history");
  expect(init).toMatchObject({
    method: "GET",
    headers: { Authorization: "Bearer secret-token" },
  });
});

test("an opened Narration carries its Source and its gaps", async () => {
  const { client, fetcher, ready } = readyClient(async () =>
    json({
      version: 1,
      ...HISTORY_ENTRY,
      source: "Read locally.",
      segments: [],
      gaps: [
        {
          ordinal: 1,
          sourceStart: 0,
          sourceEnd: 4,
          errorCode: "synthesis_failed",
          createdAt: "2026-08-26T00:00:00+00:00",
        },
      ],
    }),
  );
  await ready;

  const narration = await client.openNarration("n 1");

  expect(narration.source).toBe("Read locally.");
  expect(narration.gaps).toHaveLength(1);
  expect(fetcher.mock.calls[1][0]).toBe("http://127.0.0.1:51234/v1/history/n%201");
});

test("a reply from an Engine speaking another version is refused, not rendered", async () => {
  const { client, ready } = readyClient(async () =>
    json({ version: 2, history: [] }),
  );
  await ready;

  await expect(client.listHistory()).rejects.toThrow(
    "The Engine sent a reply this app cannot read.",
  );
});

test("resuming a stored Narration asks its own route to play it again", async () => {
  const { client, fetcher, ready } = readyClient(async () =>
    json({ version: 1, narrationId: "n-1", status: "accepted" }, 202),
  );
  await ready;

  await client.resumeNarration("n-1", "advanced");

  const [url, init] = fetcher.mock.calls[1];
  expect(url).toBe("http://127.0.0.1:51234/v1/history/n-1/resume?mode=advanced");
  expect(init).toMatchObject({ method: "POST" });

  await client.resumeNarration("n-1", "simple", { paused: true });
  expect(fetcher.mock.calls[2][0]).toBe(
    "http://127.0.0.1:51234/v1/history/n-1/resume?mode=simple&paused=true",
  );
});

test("a refused resume surfaces the Engine's own sentence", async () => {
  const { client, ready } = readyClient(async () =>
    json(
      {
        error: {
          version: 1,
          code: "model_not_installed",
          message: "That Narration's Voice Model is not downloaded.",
        },
      },
      409,
    ),
  );
  await ready;

  await expect(client.resumeNarration("n-1", "advanced")).rejects.toThrow(
    "That Narration's Voice Model is not downloaded.",
  );
});

test("deleting a Narration reports the disk the Engine actually freed", async () => {
  const { client, fetcher, ready } = readyClient(async () =>
    json({ version: 1, narrationId: "n-1", deleted: true, audioBytesFreed: 1048576 }),
  );
  await ready;

  await expect(client.deleteNarration("n-1")).resolves.toEqual({
    narrationId: "n-1",
    audioBytesFreed: 1048576,
  });
  expect(fetcher.mock.calls[1][1]).toMatchObject({ method: "DELETE" });
});

test("a delete that reports no number frees nothing rather than NaN", async () => {
  const { client, ready } = readyClient(async () =>
    json({ version: 1, narrationId: "n-1", deleted: true }),
  );
  await ready;

  await expect(client.deleteNarration("n-1")).resolves.toEqual({
    narrationId: "n-1",
    audioBytesFreed: 0,
  });
});

test("a Catalog reply the shell cannot read is refused, not half-rendered", async () => {
  const { client, ready } = readyClient(async () => json({ version: 1 }));
  await ready;

  await expect(client.listCatalog()).rejects.toThrow(
    "The Engine sent a Catalog this app cannot read.",
  );
});

test("a Catalog from an Engine that predates licence terms is refused the same way", async () => {
  const { client, ready } = readyClient(async () =>
    json({
      version: 1,
      defaultModelId: "kokoro:82m",
      models: [{ id: "kokoro:82m", name: "Kokoro", license: "Apache-2.0" }],
    }),
  );
  await ready;

  await expect(client.listCatalog()).rejects.toThrow(
    "The Engine sent a Catalog this app cannot read.",
  );
});

const licenseTerms = {
  id: "Apache-2.0",
  name: "Apache 2.0",
  bindsReader: false,
  credit: null,
  attribution: {
    creator: "Acme Audio",
    copyrightNotice: "Copyright 2026 Acme Audio",
    source: "https://huggingface.co/x",
    warrantyNotice: "No warranties are given.",
    modified: false,
  },
  text: "Apache License…",
};

const QUALIFIED_VOICE = {
  simple: true,
  id: "af_heart",
  name: "Heart",
  language: "en-US",
  preview: null,
};

const catalogUnder = (terms: unknown) =>
  json({
    version: 1,
    defaultModelId: "kokoro:82m",
    models: [
      {
        id: "kokoro:82m",
        name: "Kokoro",
        license: "Apache-2.0",
        licenseTerms: terms,
        supportModels: [],
        voices: [QUALIFIED_VOICE],
      },
    ],
  });

test.each([
  ["no text", { ...licenseTerms, text: undefined }],
  ["a bindsReader that is not a boolean", { ...licenseTerms, bindsReader: "yes" }],
  ["a credit that is neither a line nor null", { ...licenseTerms, credit: 1 }],
  ["no attribution", { ...licenseTerms, attribution: undefined }],
  [
    "an attribution missing its source",
    {
      ...licenseTerms,
      attribution: {
        creator: "Acme Audio",
        copyrightNotice: "Copyright 2026 Acme Audio",
        warrantyNotice: "No warranties are given.",
        modified: false,
      },
    },
  ],
])("a Catalog whose licence terms have %s is refused the same way", async (_, terms) => {
  const { client, ready } = readyClient(async () => catalogUnder(terms));
  await ready;

  await expect(client.listCatalog()).rejects.toThrow(
    "The Engine sent a Catalog this app cannot read.",
  );
});

test.each([
  ["no qualification", { ...QUALIFIED_VOICE, simple: undefined }],
  ["a qualification that is not a boolean", { ...QUALIFIED_VOICE, simple: "yes" }],
])("a Catalog whose Voices carry %s is refused", async (_, voice) => {
  const { client, ready } = readyClient(async () => json({
    version: 1,
    defaultModelId: "kokoro:82m",
    models: [{ id: "kokoro:82m", licenseTerms, supportModels: [], voices: [voice] }],
  }));
  await ready;

  await expect(client.listCatalog()).rejects.toThrow(
    "The Engine sent a Catalog this app cannot read.",
  );
});

test("a Catalog whose licence terms are whole is read", async () => {
  const { client, ready } = readyClient(async () => catalogUnder(licenseTerms));
  await ready;

  const catalog = await client.listCatalog();
  expect(catalog.models[0].licenseTerms).toEqual(licenseTerms);
});

test.each([undefined, null, {}, [{ name: "Word alignment" }]])(
  "a Catalog with unreadable shared model terms is refused (%j)",
  async (supportModels) => {
    const { client, ready } = readyClient(async () => json({
      version: 1,
      defaultModelId: "kokoro:82m",
      models: [{ id: "kokoro:82m", licenseTerms, supportModels, voices: [QUALIFIED_VOICE] }],
    }));
    await ready;
    await expect(client.listCatalog()).rejects.toThrow(
      "The Engine sent a Catalog this app cannot read.",
    );
  },
);

test("a download stream that cannot be reached says so once, not twice a second", async () => {
  const controller = new AbortController();
  let attempts = 0;
  const client = createEngineClient({
    invoke: async () => null,
    fetch: async () => {
      throw new Error("the stream is not there");
    },
    delay: async () => {
      attempts += 1;
      if (attempts === 3) controller.abort();
    },
  });
  const lost: number[] = [];

  await client.watchDownloads({ onLost: () => lost.push(attempts) }, controller.signal);

  expect(attempts).toBe(3);
  expect(lost).toEqual([0]);
});

const downloadClient = (
  handle: (input: RequestInfo | URL) => Promise<Response>,
  onRetry: (attempts: number) => void,
) => {
  const watching = new AbortController();
  let attempts = 0;
  const client = createEngineClient({
    invoke: async (command) =>
      command === "engine_status" ? { state: "ready", port: 51234 } : CONFIG,
    fetch: async (input) => {
      if (String(input).endsWith("/v1/events")) {
        watching.abort();
        return streamResponse();
      }
      return handle(input);
    },
    delay: async () => {
      attempts += 1;
      onRetry(attempts);
    },
  });
  return { client, ready: client.watch({}, watching.signal) };
};

test("a download stream the Engine refuses says so once, not twice a second", async () => {
  // The latch used to be cleared by the response arriving rather than by a
  // snapshot, so an Engine that refused every connect re-announced the loss
  // on every retry.
  const controller = new AbortController();
  const retries: number[] = [];
  const { client, ready } = downloadClient(
    async () => new Response("no", { status: 401 }),
    (attempts) => {
      retries.push(attempts);
      if (attempts === 3) controller.abort();
    },
  );
  await ready;
  const lost: number[] = [];

  await client.watchDownloads(
    { onLost: () => lost.push(retries.length) },
    controller.signal,
  );

  expect(retries).toEqual([1, 2, 3]);
  expect(lost).toEqual([0]);
});

test("progress arriving again is what lets the next loss be announced", async () => {
  let refuse = false;
  const controller = new AbortController();
  const { client, ready } = downloadClient(
    async () =>
      refuse
        ? new Response("no", { status: 401 })
        : streamResponse(
            'event: download\ndata: {"version":1,"modelId":"kokoro:82m",',
            '"phase":"idle","bytesDownloaded":0,"bytesTotal":0,"error":null}\n\n',
          ),
    () => {},
  );
  await ready;
  const lost: string[] = [];

  await client.watchDownloads(
    {
      onDownload: () => {
        refuse = true;
      },
      onLost: () => {
        lost.push("lost");
        controller.abort();
      },
    },
    controller.signal,
  );

  expect(lost).toEqual(["lost"]);
});

test("a watcher that has been torn down is told nothing more", async () => {
  // React's StrictMode mounts every effect twice, so the first watcher's
  // loop is still resolving an `await` after its own cleanup ran. Anything
  // it says then lands on a consumer that is gone.
  const controller = new AbortController();
  const invoke = vi.fn(async (command: string) => {
    if (command === "engine_status") {
      controller.abort();
      return { state: "starting", attempt: 1 };
    }
    return CONFIG;
  });
  const connections: Connection[] = [];

  await createEngineClient({ invoke, fetch: async () => streamResponse(), delay: async () => {} })
    .watch({ onConnection: (connection) => connections.push(connection) }, controller.signal);

  expect(connections).toEqual([]);
});

test("two watchers each hear the same failed Engine for themselves", async () => {
  let giveUp = () => {};
  const client = createEngineClient({
    invoke: async () => ({ state: "failed", reason: "uv exited 1" }),
    fetch: async () => streamResponse(),
    delay: async () => giveUp(),
  });
  const heard = async () => {
    const controller = new AbortController();
    giveUp = () => controller.abort();
    const seen: Connection[] = [];
    await client.watch(
      {
        onConnection: (connection) => {
          seen.push(connection);
          controller.abort();
        },
      },
      controller.signal,
    );
    return seen;
  };

  expect(await heard()).toHaveLength(1);
  expect(await heard()).toHaveLength(1);
});

test("the Voice the reader chose travels with the Narration it starts", async () => {
  const { client, fetcher, ready } = readyClient(
    async () => new Response(null, { status: 202 }),
  );
  await ready;

  await client.narrate("Read this one aloud.", {
    modelId: "qwen3-tts:0.6b",
    voiceId: "Chelsie",
  }, "advanced");

  const [, request] = fetcher.mock.calls.find(([input]) =>
    String(input).endsWith("/v1/audio/speech"),
  )!;
  expect(JSON.parse(String(request?.body))).toEqual({
    input: "Read this one aloud.",
    mode: "advanced",
    model: "qwen3-tts:0.6b",
    voice: "Chelsie",
  });
});

test("choosing a Voice stores it, and the Engine's answer is what stuck", async () => {
  const { client, fetcher, ready } = readyClient(async () =>
    json({ version: 1, modelId: "qwen3-tts:0.6b", voiceId: "Chelsie" }),
  );
  await ready;

  await expect(
    client.selectVoice({ modelId: "qwen3-tts", voiceId: "Chelsie" }),
  ).resolves.toEqual({ modelId: "qwen3-tts:0.6b", voiceId: "Chelsie" });

  const [url, request] = fetcher.mock.calls[1];
  expect(url).toBe("http://127.0.0.1:51234/v1/settings/voice");
  expect(request?.method).toBe("PATCH");
  expect(JSON.parse(String(request?.body))).toEqual({
    modelId: "qwen3-tts",
    voiceId: "Chelsie",
  });
});

test("a Voice reply the shell cannot name is refused, not rendered as undefined", async () => {
  const { client, ready } = readyClient(async () => json({ version: 1 }));
  await ready;

  await expect(client.voiceSelection()).rejects.toThrow(
    "The Engine named a Voice this app cannot read.",
  );
});

test("Advanced controls and takes use authenticated versioned routes", async () => {
  const controller = new AbortController();
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input).endsWith("/v1/events")) {
      controller.abort();
      return streamResponse();
    }
    return new Response(JSON.stringify({ version: 1, overrides: { steps: 12 }, effectiveValues: { steps: 12, seed: null } }));
  });
  const client = createEngineClient({ invoke: async (command) => command === "engine_status" ? { state: "ready", port: CONFIG.port } : CONFIG, fetch: fetcher });
  await client.watch({}, controller.signal);
  const voice = { modelId: "supertonic:66m", voiceId: "M1" };
  expect((await client.controls(voice)).overrides).toEqual({ steps: 12 });
  await client.setControls(voice, { steps: 20 });
  await client.selectTake("saved", 2, "reroll");
  expect(fetcher).toHaveBeenCalledWith(`http://127.0.0.1:${CONFIG.port}/v1/settings/controls?modelId=supertonic%3A66m&voiceId=M1`, expect.objectContaining({ headers: { Authorization: "Bearer secret-token" } }));
  expect(fetcher).toHaveBeenCalledWith(`http://127.0.0.1:${CONFIG.port}/v1/settings/controls`, expect.objectContaining({ method: "PATCH", headers: { Authorization: "Bearer secret-token", "Content-Type": "application/json" }, body: JSON.stringify({ ...voice, overrides: { steps: 20 } }) }));
  expect(fetcher).toHaveBeenCalledWith(`http://127.0.0.1:${CONFIG.port}/v1/history/saved/take`, expect.objectContaining({ method: "POST", body: JSON.stringify({ ordinal: 2, action: "reroll" }) }));
  fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ version: 1, overrides: [], effectiveValues: {} })));
  await expect(client.controls(voice)).rejects.toThrow("controls this app cannot read");
});
