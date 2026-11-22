import { useId } from "react";

/**
 * Splitch wordmark — "SPLITCH" in Unbounded Bold (SIL OFL 1.1), rendered as exact vector glyph
 * outlines with the same Shear Cut as the emblem: the upper half follows the text color and the
 * lower half follows the live --accent theme color, offset +/-0.75 units either side of the cut.
 * Pure inline SVG: zero font files, zero CSP font-src dependency, crisp at any DPI.
 */
const WORDMARK_PATH =
  "M0.6 11.6H4.4Q4.5 12.1 5 12.5Q5.4 13 6.1 13.2Q6.9 13.4 7.8 13.4Q9.2 13.4 10 13.1Q10.8 12.7 10.8 12Q10.8 11.5 10.3 11.2Q9.9 10.9 8.6 10.8L6.1 10.6Q3.3 10.3 2.1 9.3Q0.8 8.3 0.8 6.5Q0.8 5.1 1.6 4.2Q2.4 3.2 3.9 2.7Q5.4 2.2 7.4 2.2Q9.3 2.2 10.8 2.8Q12.3 3.3 13.2 4.3Q14.1 5.3 14.2 6.7H10.4Q10.3 6.2 10 5.8Q9.6 5.5 8.9 5.3Q8.2 5.1 7.3 5.1Q6 5.1 5.3 5.4Q4.6 5.8 4.6 6.4Q4.6 6.9 5 7.2Q5.5 7.4 6.6 7.6L9.3 7.8Q11.2 8 12.4 8.4Q13.5 8.9 14 9.7Q14.6 10.4 14.6 11.6Q14.6 13 13.7 14.1Q12.9 15.1 11.3 15.7Q9.8 16.3 7.8 16.3Q5.7 16.3 4.1 15.7Q2.5 15.1 1.6 14Q0.7 13 0.6 11.6Z M24.6 2.5Q26.2 2.5 27.5 3.1Q28.7 3.7 29.4 4.7Q30 5.8 30 7.2Q30 8.5 29.4 9.6Q28.7 10.7 27.5 11.2Q26.2 11.8 24.6 11.8H18.7V8.8H24.3Q25.2 8.8 25.7 8.4Q26.2 7.9 26.2 7.2Q26.2 6.4 25.7 5.9Q25.2 5.5 24.3 5.5H19L20.7 3.8V16H17V2.5Z M35.9 2.5V14.3L34.2 12.7H44.3V16H32.2V2.5Z M46.5 2.5H50.2V16H46.5Z M57.5 4.1H61.2V16H57.5ZM52.3 2.5H66.4V5.8H52.3Z M83.5 10.3Q83.3 12.1 82.3 13.5Q81.3 14.8 79.6 15.5Q77.9 16.3 75.8 16.3Q73.4 16.3 71.6 15.4Q69.9 14.5 68.9 12.9Q67.9 11.4 67.9 9.2Q67.9 7.1 68.9 5.6Q69.9 4 71.6 3.1Q73.4 2.2 75.8 2.2Q77.9 2.2 79.6 2.9Q81.3 3.7 82.3 5Q83.3 6.4 83.5 8.2H79.8Q79.6 7.3 79.1 6.7Q78.5 6.1 77.7 5.8Q76.9 5.4 75.8 5.4Q74.5 5.4 73.6 5.9Q72.7 6.4 72.2 7.2Q71.7 8.1 71.7 9.2Q71.7 10.4 72.2 11.3Q72.7 12.1 73.6 12.6Q74.5 13.1 75.8 13.1Q76.9 13.1 77.7 12.8Q78.5 12.5 79.1 11.8Q79.6 11.2 79.8 10.3Z M85.9 2.5H89.6V16H85.9ZM96.5 2.5H100.2V16H96.5ZM87.9 7.5H98V10.8H87.9Z";

const CUT_Y = 8.74;
const GAP = 1.25;

export function SplitchWordmark({ className }: { className?: string }) {
  const uid = useId().replace(/:/g, "");
  const topId = `splitch-wm-top-${uid}`;
  const botId = `splitch-wm-bot-${uid}`;

  return (
    <svg
      viewBox="0 1.5 104 15.5"
      role="img"
      aria-label="Splitch"
      className={className ?? "splitch-wordmark"}
      overflow="visible"
    >
      <defs>
        <clipPath id={topId}>
          <rect x="-4" y="-4" width="112" height={CUT_Y + 4} />
        </clipPath>
        <clipPath id={botId}>
          <rect x="-4" y={CUT_Y + GAP} width="112" height="30" />
        </clipPath>
      </defs>
      <g clipPath={`url(#${topId})`} transform="translate(-0.75 0)">
        <path d={WORDMARK_PATH} fill="var(--text)" />
      </g>
      <g clipPath={`url(#${botId})`} transform="translate(0.75 0)">
        <path d={WORDMARK_PATH} fill="var(--accent)" />
      </g>
    </svg>
  );
}
