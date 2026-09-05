"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Loader2, ShieldCheck } from "lucide-react";
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

export default function AccountOnboardingPage() {
  const {
    isLoading,
    localSetupAvailable,
    localSetupCanSkip,
    isNoAuthMode,
    isAuthenticated,
    isGoogleAuthEnabled,
    refreshAuth,
    refreshPermissions,
  } = useAuth();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!isLoading && !localSetupAvailable && !busy) {
      router.replace(isAuthenticated || isNoAuthMode ? "/chat" : "/login");
    }
  }, [
    isLoading,
    localSetupAvailable,
    busy,
    isAuthenticated,
    isNoAuthMode,
    router,
  ]);

  async function submit(skip: boolean) {
    if (!skip && password !== confirmation) {
      setError("Passwords do not match.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const response = await fetch(
        `/api/auth/local/setup${skip ? "/skip" : ""}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(skip ? {} : { login, password }),
        },
      );
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : "Unable to set up your account. Please try again.",
        );
      }
      setPassword("");
      setConfirmation("");
      queryClient.clear();
      await refreshAuth();
      await refreshPermissions();
      router.replace(skip && isGoogleAuthEnabled ? "/login" : "/chat");
    } catch (error) {
      setError(
        error instanceof Error
          ? error.message
          : "Unable to set up your account.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (isLoading || !localSetupAvailable)
    return (
      <div className="flex min-h-dvh items-center justify-center">
        <Loader2
          className="h-6 w-6 animate-spin"
          aria-label="Loading account setup"
        />
      </div>
    );

  return (
    <main className="flex h-dvh items-start justify-center overflow-y-auto bg-background p-4 sm:p-8">
      <Card className="my-auto w-full max-w-lg">
        <CardHeader>
          <ShieldCheck className="mb-3 h-8 w-8 text-primary" />
          <CardTitle className="text-2xl">Welcome to OpenRAG</CardTitle>
          <CardDescription className="pt-2">
            Create your local administrator account to manage this workspace and
            its users.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <p className="mb-6 text-sm text-muted-foreground">
            Creating an account requires everyone to sign in. Your password
            stays in OpenRAG; no external account is needed.
            {isGoogleAuthEnabled && " Google sign-in will remain available."}
          </p>
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              void submit(false);
            }}
          >
            <div className="space-y-2">
              <Label htmlFor="setup-login">Username</Label>
              <Input
                id="setup-login"
                autoComplete="username"
                value={login}
                onChange={(e) => setLogin(e.target.value)}
                minLength={3}
                maxLength={64}
                required
                disabled={busy}
              />
              <p className="text-xs text-muted-foreground">
                3–64 letters, digits, dots, hyphens or underscores. This account
                will be an administrator.
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="setup-password">Password</Label>
              <Input
                id="setup-password"
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                minLength={12}
                maxLength={1024}
                required
                disabled={busy}
              />
              <p className="text-xs text-muted-foreground">
                At least 12 characters.
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="setup-confirm">Confirm password</Label>
              <Input
                id="setup-confirm"
                type="password"
                autoComplete="new-password"
                value={confirmation}
                onChange={(e) => setConfirmation(e.target.value)}
                required
                disabled={busy}
              />
            </div>
            {error && (
              <p role="alert" className="text-sm text-destructive">
                {error}
              </p>
            )}
            <Button className="w-full" type="submit" disabled={busy}>
              {busy ? "Saving…" : "Create administrator and continue"}
            </Button>
          </form>
          {localSetupCanSkip && (
            <div className="mt-5 border-t pt-5">
              <Button
                className="w-full"
                variant="outline"
                onClick={() => submit(true)}
                disabled={busy}
              >
                Continue without a local account
              </Button>
              <p className="mt-2 text-xs text-muted-foreground">
                {isGoogleAuthEnabled
                  ? "Continue with your configured Google sign-in."
                  : "Anyone who can access this installation will be able to use it without signing in."}
              </p>
            </div>
          )}
        </CardContent>
      </Card>
    </main>
  );
}
