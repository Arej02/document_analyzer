"use client";

import "./IridescentLogo.css";

/**
 * Circle colours are lifted straight from the hero's Grainient background
 * (see components/hero.tsx) -- one circle per background colour, so the mark
 * reads as a solid piece of the aurora rather than a separate material.
 */
const circles = [
  {
    id: "large",
    className: "logo-circle--large",
    // Grainient color1 -- periwinkle
    light: "#d4d5e9",
    base: "#a0a1cf",
    shade: "#8081a6",
  },
  {
    id: "small",
    className: "logo-circle--small",
    // Grainient color3 -- grey
    light: "#d4d4d4",
    base: "#9f9f9f",
    shade: "#7f7f7f",
  },
  {
    id: "bottom",
    className: "logo-circle--bottom",
    // Grainient color2 -- slate
    light: "#aaaeb7",
    base: "#434a5f",
    shade: "#363b4c",
  },
];

export default function IridescentLogo({ size = 400 }) {
  return (
    <div
      className="iridescent-logo"
      style={{ width: size }}
      role="img"
      aria-label="Galvanise three-circle zinc logo"
    >
      {circles.map((circle) => (
        <div
          key={circle.id}
          className={`logo-circle ${circle.className}`}
          aria-hidden="true"
          style={{
            "--logo-light": circle.light,
            "--logo-base": circle.base,
            "--logo-shade": circle.shade,
          }}
        />
      ))}
    </div>
  );
}
