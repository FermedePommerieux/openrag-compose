import { ReactNode } from "react";

interface MessageProps {
  icon: ReactNode;
  children: ReactNode;
  actions?: ReactNode;
  isAssistant?: boolean;
  unstyledContent?: boolean;
}

export function Message({
  icon,
  children,
  actions,
  isAssistant,
  unstyledContent = false,
}: MessageProps) {
  return (
    <div className="flex min-w-0 gap-2 sm:gap-3">
      {icon}
      <div
        className={
          isAssistant && !unstyledContent
            ? "min-w-0 px-3 py-3 bg-secondary/20 rounded-2xl flex-1 sm:px-5 sm:py-4"
            : "min-w-0 flex-1"
        }
      >
        <div className="flex-1 min-w-0">{children}</div>
        {actions && <div className="flex-shrink-0 ml-2">{actions}</div>}
      </div>
    </div>
  );
}
