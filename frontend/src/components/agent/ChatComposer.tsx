import { useLayoutEffect, useRef, type ReactNode, type RefObject } from "react";
import { Icon } from "../ui/Icon";

interface ChatComposerProps {
  label: string;
  submitLabel: string;
  placeholder: string;
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  tools?: ReactNode;
  disabled?: boolean;
  sendDisabled?: boolean;
  onStop?: () => void;
  stopDisabled?: boolean;
  autoFocus?: boolean;
  maxLength?: number;
  inputRef?: RefObject<HTMLTextAreaElement | null>;
}

export default function ChatComposer({
  label, submitLabel, placeholder, value, onChange, onSubmit, tools, disabled,
  sendDisabled, onStop, stopDisabled, autoFocus, maxLength, inputRef,
}: ChatComposerProps) {
  const localRef = useRef<HTMLTextAreaElement>(null);
  const field = inputRef ?? localRef;
  useLayoutEffect(() => {
    const input = field.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  }, [value, field]);

  const canSend = Boolean(value.trim()) && !disabled && !sendDisabled && !onStop;
  return (
    <form className="ww-chat-composer" onSubmit={event => { event.preventDefault(); if (canSend) onSubmit(); }}>
      <textarea ref={field} rows={1} aria-label={label} placeholder={placeholder} autoFocus={autoFocus}
        maxLength={maxLength} disabled={disabled} value={value} onChange={event => onChange(event.target.value)}
        onKeyDown={event => {
          if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
          event.preventDefault();
          if (canSend) onSubmit();
        }} />
      <div className="ww-chat-composer-tools">
        {tools}
        {onStop ? <button type="button" className="ww-chat-send" aria-label="取消任务" title="取消任务" disabled={stopDisabled} onClick={onStop}><Icon name="stop" size={16}/></button>
          : <button type="submit" className="ww-chat-send" aria-label={submitLabel} title={submitLabel} disabled={!canSend}><Icon name="arrow-up" size={19}/></button>}
      </div>
    </form>
  );
}
