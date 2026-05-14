/**
 * CompassRose — circular compass with N/S/E/W arrows.
 *
 * Rotates with `headingDeg` (0 = North up). When `onClick` is provided the rose
 * is clickable and a tooltip nudges the user toward "reset to North". 2D Leaflet
 * can't rotate, so the 2D map passes a static headingDeg=0 with no onClick; the
 * 3D globe wires it to the live camera heading and a flyTo-north handler.
 */
export default function CompassRose({ size = 90, headingDeg = 0, onClick }) {
  const ticks = Array.from({ length: 16 }, (_, i) => {
    const angle = (i * 22.5) * Math.PI / 180
    const isCardinal = i % 4 === 0
    const r1 = isCardinal ? 43 : 46
    const r2 = 48
    return {
      x1: 50 + r1 * Math.sin(angle), y1: 50 - r1 * Math.cos(angle),
      x2: 50 + r2 * Math.sin(angle), y2: 50 - r2 * Math.cos(angle),
      w: isCardinal ? 1.5 : 0.8,
    }
  })
  // The whole graphic rotates by -headingDeg so a north-facing camera (heading=0)
  // keeps N at the top, and turning the camera right (heading=90°) makes the rose
  // rotate left so N points to the user's left.
  return (
    <svg width={size} height={size} viewBox="0 0 100 100"
         onClick={onClick}
         style={{
           filter: 'drop-shadow(0 0 5px rgba(0,0,0,0.9))',
           cursor: onClick ? 'pointer' : 'default',
           transform: `rotate(${-headingDeg}deg)`,
           transition: 'transform 0.12s linear',
         }}>
      <title>{onClick ? `Heading ${Math.round(((headingDeg % 360) + 360) % 360)}° — click to face North` : `North`}</title>
      <circle cx="50" cy="50" r="48" fill="rgba(0,0,0,0.25)" stroke="#fff" strokeWidth="0.8" strokeOpacity="0.4"/>
      {ticks.map((t, i) => (
        <line key={i} x1={t.x1} y1={t.y1} x2={t.x2} y2={t.y2}
              stroke="#fff" strokeWidth={t.w} opacity="0.5"/>
      ))}
      <polygon points="50,8 53.5,44 50,40 46.5,44" fill="#ef4444"/>
      <polygon points="50,40 53.5,44 50,48 46.5,44" fill="#fff" opacity="0.85"/>
      <polygon points="50,92 53.5,56 50,60 46.5,56" fill="#fff" opacity="0.7"/>
      <polygon points="50,60 53.5,56 50,52 46.5,56" fill="#aaa" opacity="0.7"/>
      <polygon points="92,50 56,53.5 60,50 56,46.5" fill="#fff" opacity="0.7"/>
      <polygon points="60,50 56,53.5 52,50 56,46.5" fill="#aaa" opacity="0.7"/>
      <polygon points="8,50 44,53.5 40,50 44,46.5" fill="#fff" opacity="0.7"/>
      <polygon points="40,50 44,53.5 48,50 44,46.5" fill="#aaa" opacity="0.7"/>
      <circle cx="50" cy="50" r="3.5" fill="#fff" opacity="0.9"/>
      <text x="50" y="20" textAnchor="middle" fill="#ef4444" fontSize="13" fontWeight="bold" fontFamily="sans-serif">N</text>
      <text x="50" y="88" textAnchor="middle" fill="#fff" fontSize="11" opacity="0.75" fontFamily="sans-serif">S</text>
      <text x="84" y="54" textAnchor="middle" fill="#fff" fontSize="11" opacity="0.75" fontFamily="sans-serif">E</text>
      <text x="16" y="54" textAnchor="middle" fill="#fff" fontSize="11" opacity="0.75" fontFamily="sans-serif">W</text>
    </svg>
  )
}
