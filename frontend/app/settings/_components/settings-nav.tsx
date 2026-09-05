"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/settings-tabs";
import { useAuth } from "@/contexts/auth-context";
import { useIsCloudBrand } from "@/contexts/brand-context";
import { useSettingsTabAccess } from "@/hooks/use-permissions";
import {
  canAccessConnectorAccessTab,
  canShowRbacGatedSettingsTab,
} from "@/lib/brand";
import { cn } from "@/lib/utils";

const TABS = [
  { value: "connectors", label: "Connectors" },
  { value: "providers", label: "Providers", perm: "providers:write" },
  // Agent + ingest settings write workspace config (admin-only).
  { value: "langflow", label: "Langflow", perm: "config:write" },
  // Retrieval policy writes workspace config (admin-only).
  { value: "retrieval", label: "Retrieval", perm: "config:write" },
  // Local-source archiving settings write workspace config (admin-only).
  { value: "archiving", label: "Archiving", perm: "config:write" },
  { value: "api-keys", label: "API Keys", apiKeysTab: true },
  { value: "local-users", label: "Local users" },
  {
    value: "connector-access",
    label: "Connector Settings",
    perm: "connectors:manage:access",
  },
] as const;

export function SettingsNav() {
  const pathname = usePathname();
  const router = useRouter();
  const {
    isAuthenticated,
    isNoAuthMode,
    isIbmAuthMode,
    isLoading,
    permissionsResolved,
    permissions,
    isLocalAuthEnabled,
  } = useAuth();
  const isCloudBrand = useIsCloudBrand();
  const tabAccess = useSettingsTabAccess();

  const currentTab = pathname.split("/").pop() ?? "connectors";

  const visibleTabs = TABS.filter((tab) => {
    if (tab.value === "local-users") {
      return (
        isAuthenticated &&
        isLocalAuthEnabled &&
        permissions.has("users:invite") &&
        permissions.has("roles:assign")
      );
    }
    if (tab.value === "connector-access") {
      return canAccessConnectorAccessTab(tabAccess);
    }
    if ("perm" in tab) return canShowRbacGatedSettingsTab(tab.perm, tabAccess);
    if ("apiKeysTab" in tab)
      return (isAuthenticated || isNoAuthMode) && !isIbmAuthMode;
    return true;
  });

  const _visibleTabKey = visibleTabs.map((tab) => tab.value).join("|");
  const tabIsVisible = visibleTabs.some((tab) => tab.value === currentTab);
  const fallbackTab = visibleTabs[0]?.value ?? "connectors";

  useEffect(() => {
    if (isLoading || !permissionsResolved) return;
    if (tabIsVisible) return;
    router.replace(`/settings/${fallbackTab}`);
  }, [isLoading, permissionsResolved, tabIsVisible, fallbackTab, router]);

  return (
    <Tabs value={currentTab} className="min-w-0 max-w-full">
      <TabsList
        variant={isCloudBrand ? "line" : "default"}
        aria-label="Settings sections"
        className={cn(
          "max-w-full justify-start overflow-x-auto scrollbar-hide",
          !isCloudBrand && "mb-6 p-2 rounded-full",
        )}
      >
        {visibleTabs.map((tab) => (
          <TabsTrigger
            key={tab.value}
            value={tab.value}
            onClick={() => router.push(`/settings/${tab.value}`)}
            className={cn(!isCloudBrand && "p-3 rounded-full")}
          >
            {tab.label}
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  );
}
