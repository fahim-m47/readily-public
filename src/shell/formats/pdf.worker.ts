// The pdf.js parser thread: its legacy worker build, with the one shim that
// build leaves out loaded first. `readPdf` starts one of these per file.
import "./promiseWithResolvers";
import "pdfjs-dist/legacy/build/pdf.worker.min.mjs";
