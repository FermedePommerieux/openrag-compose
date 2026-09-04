"use client";

import { Bell, Menu, X } from "lucide-react";
import { BrandSwitcher } from "@/components/brand-switcher";
import { DevRoleToggle } from "@/components/dev-role-toggle";
import Logo from "@/components/icons/openrag-logo";
import { UserNav } from "@/components/user-nav";
import { useIsCloudBrand } from "@/contexts/brand-context";
import { useTask } from "@/contexts/task-context";
import { cn } from "@/lib/utils";

interface HeaderProps {
  isMobileNavigationOpen: boolean;
  onMobileNavigationToggle: () => void;
}

export function Header({
  isMobileNavigationOpen,
  onMobileNavigationToggle,
}: HeaderProps) {
  const isCloudBrand = useIsCloudBrand();
  const { tasks, isMenuOpen, toggleMenu } = useTask();

  // Calculate active tasks for the bell icon
  const activeTasks = tasks.filter(
    (task) =>
      task.status === "pending" ||
      task.status === "running" ||
      task.status === "processing",
  );

  return (
    <header className={cn("flex w-full h-full items-center justify-between")}>
      <div className="header-start-display min-w-0 pl-2 pr-1 sm:px-4">
        <button
          type="button"
          aria-label={
            isMobileNavigationOpen ? "Close navigation" : "Open navigation"
          }
          aria-controls="mobile-navigation"
          aria-expanded={isMobileNavigationOpen}
          onClick={onMobileNavigationToggle}
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg hover:bg-muted md:hidden"
        >
          {isMobileNavigationOpen ? (
            <X className="h-5 w-5" />
          ) : (
            <Menu className="h-5 w-5" />
          )}
        </button>
        {/* Logo/Title */}
        <div className="flex min-w-0 items-center">
          <Logo className="fill-foreground" width={24} height={22} />
          <span
            className="truncate text-base font-semibold pl-2 sm:text-lg"
            style={{ fontFamily: '"IBM Plex Mono", monospace' }}
          >
            OpenRAG
          </span>
        </div>
      </div>
      <div className="header-end-division min-w-0 pl-1 pr-1 sm:px-2">
        <div className="justify-end flex min-w-0 items-center">
          {/* Knowledge Filter Dropdown */}
          {/* <KnowledgeFilterDropdown
              selectedFilter={selectedFilter}
              onFilterSelect={setSelectedFilter}
            /> */}

          {/* GitHub Star Button */}
          {/* <GitHubStarButton repo="phact/openrag" /> */}

          {/* Discord Link */}
          {/* <DiscordLink inviteCode="EqksyE2EX9" /> */}

          {process.env.NEXT_PUBLIC_IBM_THEME_DEV === "true" && (
            <div className="hidden items-center lg:flex">
              <BrandSwitcher />
              <DevRoleToggle />
              {/* Separator */}
              <div className="w-px h-6 bg-border mx-3" />
            </div>
          )}

          {/* Task Notification Bell */}
          <button
            type="button"
            onClick={toggleMenu}
            aria-label={isMenuOpen ? "Close task panel" : "Open task panel"}
            aria-expanded={isMenuOpen}
            data-testid="task-menu-toggle"
            className="relative h-8 w-8 hover:bg-muted rounded-lg flex items-center justify-center"
          >
            <Bell
              size={16}
              className={
                isCloudBrand ? "text-foreground" : "text-muted-foreground"
              }
            />
            {activeTasks.length > 0 && <div className="header-notifications" />}
          </button>

          {/* Separator */}
          <div className="hidden w-px h-6 bg-border mx-2 sm:block lg:mx-3" />

          <UserNav />
        </div>
      </div>
    </header>
  );
}
