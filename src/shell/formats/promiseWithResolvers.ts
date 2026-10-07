// `Promise.withResolvers` for WebKit 17.0 to 17.3 (macOS 14.0 to 14.3), where
// it is missing. pdf.js 6 calls it on both threads and its legacy build does
// not polyfill it, so `pdf.ts` and `pdf.worker.ts` import this before pdf.js.
declare global {
  interface PromiseConstructor {
    withResolvers?<T>(): {
      promise: Promise<T>;
      resolve: (value: T | PromiseLike<T>) => void;
      reject: (reason?: unknown) => void;
    };
  }
}

Promise.withResolvers ??= <T>() => {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

export {};
