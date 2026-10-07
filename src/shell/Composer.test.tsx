import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import Composer from "./Composer";

afterEach(cleanup);

const mount = (text: string) => {
  const onTextChange = vi.fn();
  const fetchPage = vi.fn(async (url: string) => ({
    bytes: new TextEncoder().encode(`The page at ${url}.`),
    contentType: "text/plain",
  }));
  render(
    <Composer
      text={text}
      onTextChange={onTextChange}
      canNarrate
      onNarrate={() => {}}
      fetchPage={fetchPage}
      model={null}
      notice={null}
    />,
  );
  return { onTextChange, fetchPage, source: screen.getByLabelText("Source") };
};

// What the Finder hands a drop: the files, and an entry per item that says
// whether it is a folder.
const drop = (target: HTMLElement, files: File[]) =>
  fireEvent.drop(target, {
    dataTransfer: {
      types: ["Files"],
      files,
      items: files.map(() => ({ webkitGetAsEntry: () => ({ isDirectory: false }) })),
    },
  });

test("a dropped text file replaces the draft", async () => {
  const { onTextChange, source } = mount("the old draft");
  drop(source, [new File(["A chapter from a file."], "chapter.txt", { type: "text/plain" })]);
  await waitFor(() => expect(onTextChange).toHaveBeenCalledOnce());
  expect(onTextChange).toHaveBeenCalledWith("A chapter from a file.");
});

test("two files at once keep the draft and say why", () => {
  const { onTextChange, source } = mount("the old draft");
  drop(source, [new File(["one"], "one.txt"), new File(["two"], "two.txt")]);
  expect(onTextChange).not.toHaveBeenCalled();
  expect(
    screen.getByText("Readily opens one .txt, .md, .docx, .epub or .pdf file at a time, not a folder or several files."),
  ).toBeTruthy();
});

test("a file that finishes reading after newer typing is dropped", async () => {
  const { onTextChange, source } = mount("");
  let finish: (text: string) => void = () => {};
  const slow = new File([""], "slow.txt", { type: "text/plain" });
  Object.defineProperty(slow, "text", {
    value: () =>
      new Promise<string>((resolve) => {
        finish = resolve;
      }),
  });

  drop(source, [slow]);
  expect(screen.getByText("Opening slow.txt…")).toBeTruthy();
  fireEvent.change(source, { target: { value: "typed meanwhile" } });
  await act(async () => finish("the file's words"));

  expect(onTextChange.mock.calls).toEqual([["typed meanwhile"]]);
});

test("the card stays highlighted while a file is dragged across its children", () => {
  const { source } = mount("");
  const card = source.closest(".composer")!;
  const files = { dataTransfer: { types: ["Files"] } };

  // WKWebView gives these no relatedTarget: the card is entered, then the
  // textarea, then the card is left for the textarea.
  fireEvent.dragEnter(card, files);
  fireEvent.dragEnter(source, files);
  fireEvent.dragLeave(card, files);
  expect(card.classList).toContain("composer--dropping");

  fireEvent.dragLeave(source, files);
  expect(card.classList).not.toContain("composer--dropping");
});

test("a link the reader opens replaces the draft with its page", async () => {
  const { onTextChange, fetchPage } = mount("the old draft");
  fireEvent.click(screen.getByRole("button", { name: "Open link" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Link" }), { target: { value: "example.com/story" } });
  fireEvent.click(screen.getByRole("button", { name: "Read" }));

  expect(screen.getByText("Opening the page…")).toBeTruthy();
  await waitFor(() => expect(onTextChange).toHaveBeenCalledWith("The page at https://example.com/story."));
  expect(fetchPage).toHaveBeenCalledOnce();
});

test("Escape puts the link away without fetching anything", () => {
  const { fetchPage } = mount("");
  fireEvent.click(screen.getByRole("button", { name: "Open link" }));
  const link = screen.getByRole("textbox", { name: "Link" });
  fireEvent.change(link, { target: { value: "https://example.com" } });
  fireEvent.keyDown(link, { key: "Escape" });

  expect(screen.queryByRole("textbox", { name: "Link" })).toBeNull();
  expect(fetchPage).not.toHaveBeenCalled();
});

test("a link pasted into the draft stays text and is never fetched", () => {
  const { onTextChange, fetchPage, source } = mount("");
  fireEvent.change(source, { target: { value: "https://example.com/story" } });

  expect(onTextChange).toHaveBeenCalledWith("https://example.com/story");
  expect(fetchPage).not.toHaveBeenCalled();
});
