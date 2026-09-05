"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAuth } from "@/contexts/auth-context";

export default function RequiredPasswordChange() {
  const {
    isLoading,
    passwordChangeRequired,
    isAuthenticated,
    refreshAuth,
    refreshPermissions,
  } = useAuth();
  const router = useRouter();
  const cache = useQueryClient();
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!isLoading && !passwordChangeRequired && !busy)
      router.replace(isAuthenticated ? "/chat" : "/login");
  }, [isLoading, passwordChangeRequired, isAuthenticated, busy, router]);

  if (isLoading || !passwordChangeRequired) return null;
  return (
    <main className="flex h-dvh justify-center overflow-y-auto p-4 sm:p-8">
      <Card className="my-auto w-full max-w-lg">
        <CardHeader>
          <CardTitle>Choose your password</CardTitle>
          <CardDescription>
            Replace your temporary password before accessing your workspace.
            Choose a different password with at least 12 characters.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form
            className="space-y-4"
            onSubmit={async (event) => {
              event.preventDefault();
              if (password !== confirmation) {
                setError("Passwords do not match.");
                return;
              }
              setBusy(true);
              setError("");
              try {
                const response = await fetch(
                  "/api/auth/local/password/required",
                  {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ password }),
                  },
                );
                if (!response.ok) {
                  const data = await response.json().catch(() => ({}));
                  if (response.status === 401) {
                    await refreshAuth();
                    router.replace("/login");
                    return;
                  }
                  throw new Error(
                    typeof data.detail === "string"
                      ? data.detail
                      : "Unable to change password.",
                  );
                }
                setPassword("");
                setConfirmation("");
                cache.clear();
                await refreshAuth();
                await refreshPermissions();
                router.replace("/chat");
              } catch (error) {
                setError(
                  error instanceof Error
                    ? error.message
                    : "Unable to change password.",
                );
              } finally {
                setBusy(false);
              }
            }}
          >
            <div className="space-y-2">
              <Label htmlFor="required-password">New password</Label>
              <Input
                id="required-password"
                type="password"
                autoComplete="new-password"
                minLength={12}
                maxLength={1024}
                required
                disabled={busy}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="required-password-confirm">
                Confirm password
              </Label>
              <Input
                id="required-password-confirm"
                type="password"
                autoComplete="new-password"
                required
                disabled={busy}
                value={confirmation}
                onChange={(e) => setConfirmation(e.target.value)}
              />
            </div>
            {error && (
              <p role="alert" className="text-sm text-destructive">
                {error}
              </p>
            )}
            <Button className="w-full" type="submit" disabled={busy}>
              {busy ? "Saving…" : "Save password and continue"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}
