"use client";

import { Loader2, Plus, Users } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useAuth } from "@/contexts/auth-context";

type LocalUser = {
  user_id: string;
  login: string;
  enabled: boolean;
  roles: string[];
  workspace: string;
};

async function accountRequest(path: string, method = "GET", body?: object) {
  const response = await fetch(`/api/users/local${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "Unable to update local users. Please try again.",
    );
  return data;
}

export function LocalUsersSection() {
  const { user, refreshAuth, isLocalAuthEnabled, permissions } = useAuth();
  const allowed =
    isLocalAuthEnabled &&
    permissions.has("users:invite") &&
    permissions.has("roles:assign");
  const [accounts, setAccounts] = useState<LocalUser[]>([]);
  const [roles, setRoles] = useState<string[]>([]);
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [dialog, setDialog] = useState<"create" | LocalUser | null>(null);
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [role, setRole] = useState("user");
  const [formError, setFormError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await accountRequest(`?limit=25&offset=${page * 25}`);
      setAccounts(data.users);
      setRoles(data.available_roles);
    } catch (error) {
      setError(
        error instanceof Error ? error.message : "Unable to load local users.",
      );
    } finally {
      setLoading(false);
    }
  }, [page]);

  useEffect(() => {
    if (allowed) void load();
  }, [allowed, load]);

  function closeDialog() {
    setDialog(null);
    setPassword("");
    setConfirmation("");
    setFormError("");
  }

  async function toggleAccount(account: LocalUser) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await accountRequest(`/${account.user_id}`, "PATCH", {
        enabled: !account.enabled,
      });
      setNotice(
        `${account.login} ${account.enabled ? "disabled. Active sessions have been revoked." : "enabled."}`,
      );
      await load();
    } catch (error) {
      setError(
        error instanceof Error ? error.message : "Unable to update account.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (!allowed)
    return (
      <p className="text-sm text-muted-foreground">
        Local user administration requires administrator access.
      </p>
    );

  return (
    <Card>
      <CardHeader className="gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex gap-3">
          <Users className="mt-1 h-5 w-5 text-muted-foreground" />
          <div>
            <CardTitle>Local users</CardTitle>
            <CardDescription className="mt-1">
              Manage accounts and access to this workspace. Disabling an account
              signs it out.
            </CardDescription>
          </div>
        </div>
        <Button
          onClick={() => {
            setLogin("");
            setRole("user");
            setNotice("");
            setDialog("create");
          }}
          disabled={busy || loading}
        >
          <Plus className="h-4 w-4" /> Create user
        </Button>
      </CardHeader>
      <CardContent className="space-y-4">
        {error && (
          <div role="alert" className="text-sm text-destructive">
            {error}{" "}
            <Button variant="link" onClick={load}>
              Retry
            </Button>
          </div>
        )}
        {notice && (
          <p role="status" className="text-sm">
            {notice}
          </p>
        )}
        {loading ? (
          <p role="status" className="flex items-center gap-2 text-sm">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading users…
          </p>
        ) : (
          !error &&
          (accounts.length === 0 ? (
            <p className="py-6 text-sm text-muted-foreground">
              No local users on this page.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[44rem] text-left text-sm">
                <thead>
                  <tr className="border-b text-muted-foreground">
                    <th className="py-3 pr-4 font-medium">User</th>
                    <th className="p-3 font-medium">Role / workspace</th>
                    <th className="p-3 font-medium">Status</th>
                    <th className="py-3 pl-3 text-right font-medium">
                      Actions
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {accounts.map((account) => (
                    <tr
                      key={account.user_id}
                      className="border-b last:border-0"
                    >
                      <td className="whitespace-nowrap py-4 pr-4">
                        <span className="font-medium">{account.login}</span>
                        {account.user_id === user?.user_id && (
                          <span className="ml-2 text-xs text-muted-foreground">
                            You
                          </span>
                        )}
                        <p className="mt-1 select-all font-mono text-xs text-muted-foreground">
                          {account.user_id}
                        </p>
                      </td>
                      <td className="p-3">
                        <p>{account.roles.join(", ")}</p>
                        <p className="text-xs text-muted-foreground">
                          {account.workspace}
                        </p>
                      </td>
                      <td className="p-3">
                        {account.enabled ? "Enabled" : "Disabled"}
                      </td>
                      <td className="py-3 pl-3">
                        <div className="flex justify-end gap-2">
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={busy}
                            onClick={() => setDialog(account)}
                            aria-label={`Reset password for ${account.login}`}
                          >
                            Reset password
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={busy || account.user_id === user?.user_id}
                            onClick={() => toggleAccount(account)}
                            aria-label={`${account.enabled ? "Disable" : "Enable"} ${account.login}`}
                          >
                            {account.enabled ? "Disable" : "Enable"}
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))
        )}
        <div className="flex items-center justify-end gap-3">
          <Button
            variant="outline"
            size="sm"
            disabled={page === 0 || loading || busy}
            onClick={() => setPage(page - 1)}
          >
            Previous
          </Button>
          <span className="text-sm text-muted-foreground">Page {page + 1}</span>
          <Button
            variant="outline"
            size="sm"
            disabled={accounts.length < 25 || loading || busy || !!error}
            onClick={() => setPage(page + 1)}
          >
            Next
          </Button>
        </div>
      </CardContent>
      <Dialog
        open={dialog !== null}
        onOpenChange={(open) => {
          if (!open && !busy) closeDialog();
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {dialog === "create"
                ? "Create local user"
                : `Reset password for ${dialog?.login}`}
            </DialogTitle>
            <DialogDescription>
              {dialog === "create"
                ? "Create an account with access determined by its workspace role."
                : "Existing sessions will be revoked. Share the new password securely. Resetting your own password signs you out."}
            </DialogDescription>
          </DialogHeader>
          <form
            className="space-y-4"
            onSubmit={async (event) => {
              event.preventDefault();
              if (password !== confirmation) {
                setFormError("Passwords do not match.");
                return;
              }
              if (!dialog) return;
              setBusy(true);
              setFormError("");
              try {
                if (dialog === "create") {
                  await accountRequest("", "POST", { login, password, role });
                  setNotice(`${login} created.`);
                } else {
                  await accountRequest(`/${dialog.user_id}/password`, "POST", {
                    password,
                  });
                  setNotice(
                    `Password reset for ${dialog.login}. Active sessions have been revoked.`,
                  );
                  if (dialog.user_id === user?.user_id) {
                    closeDialog();
                    await refreshAuth();
                    return;
                  }
                }
                closeDialog();
                await load();
              } catch (error) {
                setFormError(
                  error instanceof Error
                    ? error.message
                    : "Unable to save account.",
                );
              } finally {
                setBusy(false);
              }
            }}
          >
            {dialog === "create" && (
              <>
                <div className="space-y-2">
                  <Label htmlFor="new-local-login">Username</Label>
                  <Input
                    id="new-local-login"
                    value={login}
                    onChange={(e) => setLogin(e.target.value)}
                    autoComplete="off"
                    minLength={3}
                    maxLength={64}
                    pattern="[A-Za-z0-9][A-Za-z0-9_.\-]{2,63}"
                    required
                  />
                  <p className="text-xs text-muted-foreground">
                    3–64 letters, digits, dots, hyphens or underscores.
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="new-local-role">Workspace role</Label>
                  <Select value={role} onValueChange={setRole}>
                    <SelectTrigger id="new-local-role">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {roles.map((name) => (
                        <SelectItem key={name} value={name}>
                          {name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </>
            )}
            <div className="space-y-2">
              <Label htmlFor="new-local-password">New password</Label>
              <Input
                id="new-local-password"
                type="password"
                autoComplete="new-password"
                minLength={12}
                maxLength={1024}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
              <p className="text-xs text-muted-foreground">
                At least 12 characters.
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="confirm-local-password">Confirm password</Label>
              <Input
                id="confirm-local-password"
                type="password"
                autoComplete="new-password"
                value={confirmation}
                onChange={(e) => setConfirmation(e.target.value)}
                required
              />
            </div>
            {formError && (
              <p role="alert" className="text-sm text-destructive">
                {formError}
              </p>
            )}
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                disabled={busy}
                onClick={closeDialog}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={busy}>
                {busy
                  ? "Saving…"
                  : dialog === "create"
                    ? "Create user"
                    : "Reset password"}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
