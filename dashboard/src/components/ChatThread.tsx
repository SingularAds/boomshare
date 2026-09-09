import { useEffect, useRef } from "react";
import type { ChatMessage, Thread } from "../types";
import { clock, day, words } from "../format";
import { Empty } from "./ui";

/**
 * WhatsApp delivery ticks: one tick sent, two delivered, two blue/green read.
 */
function Ticks({ message }: { message: ChatMessage }) {
  if (message.direction !== "outbound") return null;
  if (message.status === "failed")
    return <span className="text-rose-500 font-bold" title="Failed to send">!</span>;

  const read = Boolean(message.read_at);
  const delivered = Boolean(message.delivered_at);
  if (!read && !delivered && !message.sent_at) return null;

  return (
    <svg
      viewBox="0 0 16 11"
      className={`h-3 w-3.5 ${read ? "text-emerald-500 dark:text-emerald-400" : "opacity-60"}`}
      fill="none"
      aria-label={read ? "read" : delivered ? "delivered" : "sent"}
    >
      <path
        d="M1 5.5 4 8.5 9.5 2.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {(delivered || read) && (
        <path
          d="M6 5.5 9 8.5 14.5 2.5"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      )}
    </svg>
  );
}

function Bubble({ message }: { message: ChatMessage }) {
  const outbound = message.direction === "outbound";
  const body = message.content?.trim();

  return (
    <div className={`flex px-2 py-1 ${outbound ? "justify-end" : "justify-start"}`}>
      <div
        className="relative max-w-[85%] rounded-xl px-3 py-2 text-[13.5px] leading-relaxed shadow-[0_1px_2px_rgba(0,0,0,0.08)] sm:max-w-[540px] transition-all"
        style={{
          background: outbound ? "var(--chat-bubble-out)" : "var(--chat-bubble-in)",
          color: "var(--chat-text)",
          borderTopLeftRadius: outbound ? "12px" : "3px",
          borderTopRightRadius: outbound ? "3px" : "12px",
          borderBottomLeftRadius: "12px",
          borderBottomRightRadius: "12px",
        }}
      >
        {outbound && (
          <div className="mb-1 flex items-center gap-1.5 text-[10.5px] font-bold" style={{ color: "var(--chat-meta)" }}>
            {message.ai_generated ? (
              <span className="inline-flex items-center gap-1 rounded bg-black/5 px-1.5 py-0.5 text-[9.5px] font-extrabold uppercase tracking-wider text-emerald-800 dark:bg-white/10 dark:text-emerald-300">
                🤖 AI Agent
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 rounded bg-black/5 px-1.5 py-0.5 text-[9.5px] font-extrabold uppercase tracking-wider text-slate-700 dark:bg-white/10 dark:text-slate-300">
                👤 {message.sent_by ?? "Operator"}
              </span>
            )}
          </div>
        )}

        {body ? (
          <p className="[overflow-wrap:anywhere] whitespace-pre-wrap font-normal">{body}</p>
        ) : (
          <p className="italic opacity-60 text-xs">{words(message.message_type)} payload</p>
        )}

        <div
          className="mt-1 flex items-center justify-end gap-1 text-[10.5px] font-medium select-none"
          style={{ color: "var(--chat-meta)" }}
        >
          <span>{clock(message.created_at)}</span>
          <Ticks message={message} />
        </div>
      </div>
    </div>
  );
}

export function ChatThread({ thread }: { thread: Thread | undefined }) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [thread?.id]);

  if (!thread || thread.messages.length === 0) {
    return (
      <div className="chat-surface flex flex-1 items-center justify-center p-8">
        <Empty
          title="No messages in this conversation"
          detail="The customer contact exists but no messages have been exchanged on this WhatsApp number yet."
        />
      </div>
    );
  }

  let lastDay = "";

  return (
    <div className="chat-surface min-h-60 flex-1 space-y-1 overflow-y-auto p-4 sm:p-5">
      {thread.truncated && (
        <div className="mb-3 text-center">
          <span className="rounded-full bg-black/10 px-3 py-1 text-[11px] font-semibold text-slate-600 dark:bg-white/10 dark:text-slate-400">
            Showing most recent {thread.messages.length} messages
          </span>
        </div>
      )}
      {thread.messages.map((message) => {
        const stamp = day(message.created_at);
        const separator = stamp !== lastDay;
        lastDay = stamp;
        return (
          <div key={message.id}>
            {separator && (
              <div className="my-3 text-center">
                <span
                  className="rounded-lg px-3 py-1 text-[11px] font-bold shadow-xs tracking-wide"
                  style={{ background: "var(--chat-pill)", color: "var(--chat-meta)" }}
                >
                  {stamp}
                </span>
              </div>
            )}
            <Bubble message={message} />
          </div>
        );
      })}
      <div ref={endRef} />
    </div>
  );
}
