import { Check, Copy, ThumbsDown, ThumbsUp } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { trackButton } from "@/lib/analytics";

interface MessageActionsProps {
  content: string;
  showCopy?: boolean;
  showFeedback?: boolean;
  trackFeedback: (feedback: "like" | "dislike") => void;
}

const MessageActions = ({
  content,
  showCopy = true,
  showFeedback = true,
  trackFeedback,
}: MessageActionsProps) => {
  const [copied, setCopied] = useState(false);
  const [feedbackSelected, setFeedbackSelected] = useState<
    "like" | "dislike" | null
  >(null);
  const copiedTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (copiedTimeout.current) clearTimeout(copiedTimeout.current);
    },
    [],
  );

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(content);
      trackButton({
        action: "copy",
        elementId: "copy-message",
        namespace: "chat",
        CTA: "Copy Message",
      });
      setCopied(true);
      if (copiedTimeout.current) clearTimeout(copiedTimeout.current);
      copiedTimeout.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error("Unable to copy response");
    }
  };

  const handleFeedback = (feedback: "like" | "dislike") => {
    if (feedbackSelected === feedback) return; // Prevent multiple tracking events for the same feedback
    trackFeedback(feedback);
    setFeedbackSelected(feedback);
  };

  return (
    <div className="flex space-x-2">
      {showCopy && (
        <Button
          variant="outline"
          size="icon"
          aria-label={copied ? "Response copied" : "Copy response"}
          title={copied ? "Copied" : "Copy response"}
          className="text-muted-foreground hover:text-foreground"
          onClick={handleCopy}
        >
          {copied ? (
            <Check className="h-4 w-4" aria-hidden="true" />
          ) : (
            <Copy className="h-4 w-4" aria-hidden="true" />
          )}
        </Button>
      )}
      {showFeedback && (
        <>
          <Button
            variant="outline"
            size="icon"
            aria-label="Like"
            aria-pressed={feedbackSelected === "like"}
            className={
              feedbackSelected !== "like"
                ? "text-muted-foreground hover:text-foreground"
                : ""
            }
            onClick={() => handleFeedback("like")}
          >
            <ThumbsUp
              className={`h-4 w-4 ${feedbackSelected === "like" ? "fill-current" : ""}`}
            />
          </Button>
          <Button
            variant="outline"
            size="icon"
            aria-label="Dislike"
            aria-pressed={feedbackSelected === "dislike"}
            className={
              feedbackSelected !== "dislike"
                ? "text-muted-foreground hover:text-foreground"
                : ""
            }
            onClick={() => handleFeedback("dislike")}
          >
            <ThumbsDown
              className={`h-4 w-4 ${feedbackSelected === "dislike" ? "fill-current" : ""}`}
            />
          </Button>
        </>
      )}
    </div>
  );
};

export default MessageActions;
