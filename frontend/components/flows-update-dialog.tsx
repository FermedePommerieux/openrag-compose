"use client";

import { AlertCircle, ExternalLink } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { useDismissFlowsUpdateMutation } from "@/app/api/mutations/useDismissFlowsUpdateMutation";
import { useUpdateFlowsMutation } from "@/app/api/mutations/useUpdateFlowsMutation";
import { useGetFlowsUpdatesQuery } from "@/app/api/queries/useGetFlowsUpdatesQuery";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { usePermissions } from "@/hooks/use-permissions";
import { formatFlowName } from "@/lib/utils";

interface FlowsUpdateDialogProps {
  overrideOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
}

export function FlowsUpdateDialog({
  overrideOpen,
  onOpenChange,
}: FlowsUpdateDialogProps = {}) {
  const { can } = usePermissions();
  const canEdit = can("flows:edit");
  const { data: updates, isLoading } = useGetFlowsUpdatesQuery({
    enabled: canEdit,
  });
  const updateMutation = useUpdateFlowsMutation();
  const dismissMutation = useDismissFlowsUpdateMutation();
  const [internalIsOpen, setInternalIsOpen] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const [backupCustom, setBackupCustom] = useState(true);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const isOpen = overrideOpen ?? internalIsOpen;
  const setIsOpen = (open: boolean) => {
    setInternalIsOpen(open);
    onOpenChange?.(open);
  };

  const undismissedUpdates = updates?.filter((u) => !u.dismissed) ?? [];
  const hasUndismissed = undismissedUpdates.length > 0;
  const source = undismissedUpdates.find((update) => update.source)?.source;

  const [prevIsLoading, setPrevIsLoading] = useState(isLoading);
  const [prevHasUndismissed, setPrevHasUndismissed] = useState(hasUndismissed);

  if (isLoading !== prevIsLoading || hasUndismissed !== prevHasUndismissed) {
    setPrevIsLoading(isLoading);
    setPrevHasUndismissed(hasUndismissed);
    if (overrideOpen === undefined) {
      if (!isLoading && hasUndismissed) {
        setInternalIsOpen(true);
      } else if (!isLoading) {
        setInternalIsOpen(false);
      }
    }
  }

  const handleDismiss = async () => {
    setIsOpen(false);
    setShowConfirm(false);
    if (undismissedUpdates.length === 0) return;
    try {
      await dismissMutation.mutateAsync({
        flow_types: undismissedUpdates.map((u) => u.flow_type),
      });
    } catch (e) {
      console.error("Failed to dismiss flow updates", e);
    }
  };

  const handleInitialUpdateClick = () => {
    setIsOpen(false);
    setShowConfirm(true);
  };

  const handleConfirmUpdate = async () => {
    if (undismissedUpdates.length === 0) return;
    setErrorMessage(null);
    const flowTypes = undismissedUpdates.map((u) => u.flow_type);

    try {
      const results = await updateMutation.mutateAsync({
        flow_types: flowTypes,
        backup_custom: backupCustom,
      });

      const failed = results.filter((r) => !r.success);
      if (failed.length > 0) {
        const errorText = failed
          .map(
            (f) =>
              `${formatFlowName(f.flow_type)}: ${f.error || "Update failed"}`,
          )
          .join("; ");
        setErrorMessage(errorText);
        toast.error(`Flow update failed: ${errorText}`);
      } else {
        toast.success("Flows updated successfully");
        setShowConfirm(false);
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Failed to update flows";
      setErrorMessage(msg);
      toast.error(msg);
    }
  };

  if (
    !can("flows:edit") ||
    (overrideOpen === undefined && undismissedUpdates.length === 0)
  )
    return null;

  return (
    <>
      <Dialog open={isOpen} onOpenChange={setIsOpen}>
        <DialogContent className="sm:max-w-[540px]">
          <DialogHeader>
            <DialogTitle>OpenRAG flow update available</DialogTitle>
            <DialogDescription>
              OpenRAG will back up your customized flows first.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-2">
            {errorMessage && (
              <Alert variant="destructive">
                <AlertCircle className="h-4 w-4" />
                <AlertTitle>Update Failed</AlertTitle>
                <AlertDescription>{errorMessage}</AlertDescription>
              </Alert>
            )}

            <p className="text-sm text-muted-foreground leading-relaxed">
              This action updates only the four OpenRAG flow definitions. It
              does not update the Langflow application or container image. The
              current flows are copied first so you can inspect or reapply
              custom changes afterward.
            </p>

            {source ? (
              <div className="rounded-md border bg-muted/40 p-3 text-sm">
                <div className="font-medium">Configured release source</div>
                <div className="mt-1 text-muted-foreground break-all">
                  {source.repository} · {source.branch} ·{" "}
                  {source.revision.slice(0, 12)}
                </div>
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
                  <a
                    href={source.branch_url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 text-primary hover:underline"
                  >
                    Open published branch
                    <ExternalLink className="h-3.5 w-3.5" />
                  </a>
                  <a
                    href={source.revision_url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 text-primary hover:underline"
                  >
                    Verify exact revision
                    <ExternalLink className="h-3.5 w-3.5" />
                  </a>
                </div>
              </div>
            ) : (
              <div className="rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">
                Source: flow files installed with this OpenRAG deployment.
              </div>
            )}

            <div className="flex items-center space-x-2 pt-2">
              <Checkbox
                id="backup-custom"
                checked={backupCustom}
                onCheckedChange={(checked) => setBackupCustom(!!checked)}
              />
              <label
                htmlFor="backup-custom"
                className="text-sm font-medium leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-70"
              >
                Back up my flows in Langflow before updating
              </label>
            </div>
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={handleDismiss}>
              Skip action
            </Button>
            <Button onClick={handleInitialUpdateClick}>
              Update OpenRAG flows
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={showConfirm} onOpenChange={setShowConfirm}>
        <DialogContent className="sm:max-w-[540px]">
          <DialogHeader>
            <DialogTitle>Confirm OpenRAG flow update</DialogTitle>
          </DialogHeader>

          <div className="py-2">
            <p className="text-sm text-muted-foreground leading-relaxed">
              The configured OpenRAG flow definitions will replace the active
              core flows. Backup copies will be created in Langflow. The
              Langflow application itself will remain unchanged. Do you want to
              continue?
            </p>
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setShowConfirm(false);
                setIsOpen(true);
              }}
            >
              Cancel
            </Button>
            <Button
              onClick={handleConfirmUpdate}
              disabled={updateMutation.isPending}
            >
              {updateMutation.isPending ? "Updating..." : "Continue update"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
