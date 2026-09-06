export type IconName = 'more' | 'spotify' | 'home' | 'search' | 'library' | 'play' | 'pause' | 'next' | 'previous' | 'shuffle' | 'repeat' | 'device' | 'volume' | 'back' | 'down' | 'queue' | 'settings' | 'plus' | 'check' | 'music' | 'heart' | 'close' | 'external' | 'pin' | 'artist' | 'podcast' | 'clock' | 'up';
const paths: Record<Exclude<IconName, 'spotify'>, string> = {
  more: 'M5 12h.01M12 12h.01M19 12h.01',
  pin: 'm16 3 5 5-4 2-3 5-5-5 5-3ZM9 15l-6 6',
  artist: 'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM4 22v-3a8 8 0 0 1 16 0v3',
  podcast: 'M15 7a3 3 0 1 1-6 0 3 3 0 0 1 6 0ZM8 14a4 4 0 0 1 8 0l-1 7H9ZM5 13a9 9 0 1 1 14 0',
  clock: 'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0ZM12 6v6l4 3',
  up: 'm4 16 8-8 8 8',
  home: 'M3 10 12 3l9 7v11h-6v-7H9v7H3Z',
  search: 'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z',
  library: 'M4 3v18M9 3v18M15 4l5 16',
  play: 'm8 4 13 8-13 8Z', pause: 'M8 5v14M16 5v14',
  next: 'm4 5 11 7L4 19ZM19 5v14', previous: 'm20 5-11 7 11 7ZM5 5v14',
  shuffle: 'M3 6h3c4 0 8 12 12 12h3m-4-4 4 4-4 4M3 18h3c1.4 0 2.8-1.5 4.2-3.5M14 9.5C15.5 7.5 17 6 18 6h3m-4-4 4 4-4 4',
  repeat: 'm17 2 4 4-4 4M3 11V9a3 3 0 0 1 3-3h15M7 22l-4-4 4-4m14-1v2a3 3 0 0 1-3 3H3',
  device: 'M3 4h12v11H3Zm3 15h6m-3-4v4m9-10h5v12h-5Z',
  volume: 'M3 9h4l5-5v16l-5-5H3Zm13-2a7 7 0 0 1 0 10m3-13a11 11 0 0 1 0 16',
  back: 'm15 4-8 8 8 8', down: 'm4 8 8 8 8-8',
  queue: 'M3 5h14M3 10h14M3 15h8m5-1 6 4-6 4Z',
  settings: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm-2-6h4l1 3 3 1 3-1 2 4-2 2v3l2 2-2 4-3-1-3 1-1 3h-4l-1-3-3-1-3 1-2-4 2-2v-3L1 9l2-4 3 1 3-1Z',
  plus: 'M12 4v16M4 12h16', check: 'm4 12 5 5L20 6',
  music: 'M9 18V5l11-2v13M9 18c0 2-2 3-4 3s-3-1-3-2 2-3 4-3 3 1 3 2Zm11-2c0 2-2 3-4 3s-3-1-3-2 2-3 4-3 3 1 3 2Z',
  heart: 'M12 21 3 12C-3 4 7-1 12 6c5-7 15-2 9 6Z',
  close: 'm5 5 14 14M5 19 19 5', external: 'M14 3h7v7m0-7L10 14M10 3H3v18h18v-7',
};
export function Icon({ name, size = 22, className }: { name: IconName; size?: number; className?: string }) {
  if (name === 'spotify') return <svg className={className} aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="12" /><g fill="none" stroke="#121212" strokeLinecap="round"><path strokeWidth="2" d="M5.1 8.5c4.8-1.4 9.4-.9 13.8 1.5"/><path strokeWidth="1.7" d="M5.8 12c4-1.1 8.2-.6 12 1.4"/><path strokeWidth="1.5" d="M6.7 15.4c3.3-.9 6.6-.5 9.6 1.1"/></g></svg>;
  return <svg className={className} aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill={['play', 'next', 'previous'].includes(name) ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth={name === 'pause' ? 4 : 1.8} strokeLinecap="round" strokeLinejoin="round"><path d={paths[name]}/></svg>;
}
