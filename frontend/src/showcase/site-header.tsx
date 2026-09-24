"use client"

import IridescentLogo from "./IridescentLogo"

export function SiteHeader() {
  return (
    <header
      className="sticky inset-x-0 top-0 z-40 border-b border-border bg-background"
    >
      {/* Clips the entrance animation: the circles start 80-110px outside the
          mark, so without this they are visible flying in above and below the
          bar. Nothing of the resting logo reaches the bar's edges. */}
      <div className="flex h-[88px] items-center gap-1 overflow-hidden px-6 sm:px-12">
        {/* Logo */}
        <div className="logo-entrance relative flex h-[84px] w-[60px] shrink-0 items-center justify-center overflow-visible">
          <IridescentLogo size={84} />
        </div>

        {/* Wordmark */}
        <span
          data-wordmark
          className="brand-wordmark text-[2.15rem] leading-none tracking-tight whitespace-nowrap text-foreground"
        >
          Galvanise
        </span>
      </div>
    </header>
  )
}
