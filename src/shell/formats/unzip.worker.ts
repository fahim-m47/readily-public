import { unzipEntries, type Want } from "./unzip";

// The worker behind `unzip`: one archive in, its wanted entries out.
self.onmessage = (event: MessageEvent<{ bytes: Uint8Array; want: Want }>) => {
  self.postMessage(unzipEntries(event.data.bytes, event.data.want));
};
