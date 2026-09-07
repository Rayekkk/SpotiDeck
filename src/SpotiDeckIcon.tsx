/** SpotiDeck's own handheld/music mark, independent of Spotify's branding. */
export function SpotiDeckIcon() {
  return <svg aria-hidden="true" width={22} height={22} viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
    <rect x={1} y={5} width={22} height={14} rx={4}/>
    <path d="M5 10v4m-2-2h4m8-4v7"/>
    <path d="m15 8-4 1v7"/>
    <ellipse cx={9.5} cy={16} rx={1.5} ry={1} fill="currentColor" stroke="none"/>
    <ellipse cx={13.5} cy={15} rx={1.5} ry={1} fill="currentColor" stroke="none"/>
    <circle cx={19} cy={10.5} r={0.8} fill="currentColor" stroke="none"/>
    <circle cx={20.5} cy={13.5} r={0.8} fill="currentColor" stroke="none"/>
  </svg>;
}
