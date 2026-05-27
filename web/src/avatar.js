(function () {
  // Palette source of truth: design.md §1.2 (12 keys, ordered).
  const PALETTE = [
    { key: 'slate',   bg: '#475569', ring: '#334155' },
    { key: 'indigo',  bg: '#4f46e5', ring: '#3730a3' },
    { key: 'rose',    bg: '#e11d48', ring: '#9f1239' },
    { key: 'amber',   bg: '#d97706', ring: '#92400e' },
    { key: 'emerald', bg: '#059669', ring: '#065f46' },
    { key: 'violet',  bg: '#7c3aed', ring: '#5b21b6' },
    { key: 'sky',     bg: '#0284c7', ring: '#075985' },
    { key: 'fuchsia', bg: '#c026d3', ring: '#86198f' },
    { key: 'teal',    bg: '#0d9488', ring: '#115e59' },
    { key: 'lime',    bg: '#65a30d', ring: '#3f6212' },
    { key: 'orange',  bg: '#ea580c', ring: '#9a3412' },
    { key: 'pink',    bg: '#db2777', ring: '#9d174d' },
  ];

  function fnv1a(input) {
    const s = String(input || '');
    let hash = 0x811c9dc5;
    for (let i = 0; i < s.length; i += 1) {
      hash ^= s.charCodeAt(i);
      hash = Math.imul(hash, 0x01000193) >>> 0;
    }
    return hash >>> 0;
  }

  function firstGlyph(agentId) {
    const chars = Array.from(String(agentId || '?').trim());
    return (chars[0] || '?').toUpperCase();
  }

  function getDefaultAvatar(agentId, emoji) {
    // design.md D-06: hash on emoji (stable same-emoji-same-color);
    // fall back to agent id when no emoji present.
    const id = String(agentId || '');
    const e = String(emoji || '').trim();
    const slot = PALETTE[fnv1a(e || id) % PALETTE.length];
    const glyph = Array.from(e)[0] || firstGlyph(id);
    return {
      key: slot.key,
      pngPath: `/assets/avatars/default/${slot.key}.png`,
      glyph,
      bg: slot.bg,
      ring: slot.ring,
    };
  }

  window.OpenForgeAvatar = { PALETTE, fnv1a, getDefaultAvatar };
})();
