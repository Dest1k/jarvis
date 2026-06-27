/**
 * ws.ts — типобезопасный клиент WebSocket для дашборда JARVIS-OS.
 *
 * Предоставляет автоматическое переподключение, разбор JSON/бинарных кадров
 * и подписку на сообщения каналов ядра FastAPI (/ws/deploy, /ws/chat,
 * /ws/audio, /ws/desktop, /ws/hitl).
 */

export type JarvisMessage = Record<string, unknown> & { type?: string };

export interface JarvisSocketOptions {
  /** Колбэк на JSON-сообщения. */
  onJson?: (msg: JarvisMessage) => void;
  /** Колбэк на бинарные кадры (аудио/кадры десктопа). */
  onBinary?: (data: ArrayBuffer) => void;
  /** Колбэк на изменение состояния соединения. */
  onState?: (state: "connecting" | "open" | "closed") => void;
  /** Базовый интервал переподключения, мс. */
  reconnectMs?: number;
}

/** Базовый URL ядра. По умолчанию — текущий хост, порт 8000. */
export function coreBase(): string {
  if (typeof window === "undefined") return "ws://localhost:8000";
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const host = process.env.NEXT_PUBLIC_CORE_HOST || `${window.location.hostname}:8000`;
  return `${proto}://${host}`;
}

export class JarvisSocket {
  private url: string;
  private opts: JarvisSocketOptions;
  private ws: WebSocket | null = null;
  private closedByUser = false;
  private backoff: number;

  constructor(path: string, opts: JarvisSocketOptions = {}) {
    this.url = `${coreBase()}${path}`;
    this.opts = opts;
    this.backoff = opts.reconnectMs ?? 1500;
  }

  connect(): void {
    this.opts.onState?.("connecting");
    const ws = new WebSocket(this.url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;

    ws.onopen = () => {
      this.backoff = this.opts.reconnectMs ?? 1500;
      this.opts.onState?.("open");
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        try {
          this.opts.onJson?.(JSON.parse(ev.data) as JarvisMessage);
        } catch {
          this.opts.onJson?.({ type: "raw", text: ev.data });
        }
      } else {
        this.opts.onBinary?.(ev.data as ArrayBuffer);
      }
    };
    ws.onclose = () => {
      this.opts.onState?.("closed");
      if (!this.closedByUser) {
        setTimeout(() => this.connect(), this.backoff);
        this.backoff = Math.min(this.backoff * 1.8, 15000);
      }
    };
    ws.onerror = () => ws.close();
  }

  sendJson(msg: JarvisMessage): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  sendBinary(data: ArrayBuffer | Blob): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(data);
    }
  }

  close(): void {
    this.closedByUser = true;
    this.ws?.close();
  }
}
