import html2canvas from "html2canvas";

function termElement(playerEl: HTMLElement): HTMLElement {
  return playerEl.querySelector<HTMLElement>(".ap-term") ?? playerEl;
}

/** PNG de l'écran courant du terminal (rendu DOM du player). */
export async function capturePng(playerEl: HTMLElement): Promise<Blob> {
  const target = termElement(playerEl);
  const bg = getComputedStyle(target).backgroundColor || "#121212";
  const canvas = await html2canvas(target, {
    backgroundColor: bg,
    scale: Math.max(2, window.devicePixelRatio || 1),
    logging: false,
    useCORS: false,
  });
  return new Promise((resolve, reject) =>
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("capture vide"))), "image/png"),
  );
}

export function downloadBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function copyPng(blob: Blob): Promise<void> {
  if (!("ClipboardItem" in window) || !navigator.clipboard?.write) {
    throw new Error("copie d'image non supportée par ce navigateur");
  }
  await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
}

/** Texte de l'écran tel qu'affiché (lignes du player), ou null. */
export function screenTextFromDom(playerEl: HTMLElement): string | null {
  const lines = playerEl.querySelectorAll<HTMLElement>(".ap-term-text .ap-line");
  if (!lines.length) return null;
  const text = Array.from(lines, (l) => (l.textContent ?? "").replace(/ /g, " ").trimEnd()).join("\n");
  return text.replace(/\n+$/, "");
}
