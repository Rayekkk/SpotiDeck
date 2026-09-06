// Center preserves both sources at full volume; moving away only attenuates one.
export function balanceVolumes(position: number) {
  const clamped = Math.max(0, Math.min(100, position));
  return { spotify: Math.min(100, clamped * 2), other: Math.min(100, (100 - clamped) * 2) };
}
