"use client";

import { Loader2 } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import GoogleLogo from "@/components/icons/google-logo";
import Logo from "@/components/icons/openrag-logo";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAuth } from "@/contexts/auth-context";

function LoginPageContent() {
  const {
    isLoading,
    isAuthenticated,
    isNoAuthMode,
    isIbmAuthMode,
    login,
    isLocalAuthEnabled,
    isGoogleAuthEnabled,
    loginLocal,
    localSetupAvailable,
    passwordChangeRequired,
  } = useAuth();
  const [localLogin, setLocalLogin] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const router = useRouter();
  const searchParams = useSearchParams();

  const requestedRedirect = searchParams.get("redirect") || "/chat";
  const redirect =
    requestedRedirect.startsWith("/") &&
    !requestedRedirect.startsWith("//") &&
    !requestedRedirect.includes("\\")
      ? requestedRedirect
      : "/chat";

  useEffect(() => {
    if (!isLoading && passwordChangeRequired) {
      router.replace("/auth/change-password");
      return;
    }
    if (!isLoading && localSetupAvailable) {
      router.replace("/onboarding/account");
      return;
    }
    if (!isLoading && (isAuthenticated || isNoAuthMode || isIbmAuthMode)) {
      router.push(redirect);
    }
  }, [
    isLoading,
    isAuthenticated,
    isNoAuthMode,
    isIbmAuthMode,
    router,
    redirect,
    localSetupAvailable,
    passwordChangeRequired,
  ]);

  if (isLoading || localSetupAvailable) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background">
        <div className="flex flex-col items-center gap-4">
          <Loader2 className="h-8 w-8 animate-spin" />
          <p className="text-muted-foreground">Loading...</p>
        </div>
      </div>
    );
  }

  if (isAuthenticated || isNoAuthMode || isIbmAuthMode) {
    return null; // Will redirect in useEffect
  }

  return (
    <div className="min-h-dvh relative flex gap-4 flex-col items-center justify-center bg-card rounded-lg m-4">
      <div className="flex flex-col items-center justify-center gap-4 z-10 ">
        <Logo className="fill-primary" width={50} height={40} />
        <div className="flex flex-col items-center justify-center gap-16">
          <h1 className="text-2xl font-medium font-chivo">
            Welcome to OpenRAG
          </h1>
          {isLocalAuthEnabled && (
            <form
              className="w-80 flex flex-col gap-4"
              onSubmit={async (event) => {
                event.preventDefault();
                setSubmitting(true);
                setError("");
                try {
                  await loginLocal(localLogin, password);
                } catch (error) {
                  setError(
                    error instanceof Error
                      ? error.message
                      : "Unable to sign in.",
                  );
                } finally {
                  setPassword("");
                  setSubmitting(false);
                }
              }}
            >
              <div className="grid gap-2">
                <Label htmlFor="local-login">Username</Label>
                <Input
                  id="local-login"
                  autoComplete="username"
                  value={localLogin}
                  onChange={(event) => setLocalLogin(event.target.value)}
                  required
                  maxLength={64}
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="local-password">Password</Label>
                <Input
                  id="local-password"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  required
                  maxLength={1024}
                />
              </div>
              {error && (
                <p role="alert" className="text-sm text-destructive">
                  {error}
                </p>
              )}
              <Button type="submit" size="lg" disabled={submitting}>
                {submitting ? "Signing in…" : "Sign in"}
              </Button>
            </form>
          )}
          {isLocalAuthEnabled && isGoogleAuthEnabled && (
            <p className="text-muted-foreground">or</p>
          )}
          {isGoogleAuthEnabled && (
            <Button onClick={login} className="w-80 gap-1.5" size="lg">
              <GoogleLogo className="h-4 w-4" />
              Continue with Google
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <div className="min-h-screen flex items-center justify-center bg-background">
          <div className="flex flex-col items-center gap-4">
            <Loader2 className="h-8 w-8 animate-spin" />
            <p className="text-muted-foreground">Loading...</p>
          </div>
        </div>
      }
    >
      <LoginPageContent />
    </Suspense>
  );
}
