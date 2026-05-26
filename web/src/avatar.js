(function () {
  const PALETTE = [
    { key: 'ember', bg: '#F36F21', ring: '#9A3412' },
    { key: 'cobalt', bg: '#2563EB', ring: '#1E3A8A' },
    { key: 'jade', bg: '#0F9F6E', ring: '#065F46' },
    { key: 'violet', bg: '#7C3AED', ring: '#4C1D95' },
    { key: 'amber', bg: '#F59E0B', ring: '#92400E' },
    { key: 'rose', bg: '#E11D48', ring: '#881337' },
    { key: 'cyan', bg: '#0891B2', ring: '#155E75' },
    { key: 'lime', bg: '#65A30D', ring: '#3F6212' },
    { key: 'indigo', bg: '#4F46E5', ring: '#312E81' },
    { key: 'coral', bg: '#EF4444', ring: '#991B1B' },
    { key: 'slate', bg: '#475569', ring: '#1E293B' },
    { key: 'mint', bg: '#14B8A6', ring: '#0F766E' },
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
    const id = String(agentId || '');
    const slot = PALETTE[fnv1a(id) % PALETTE.length];
    const glyph = Array.from(String(emoji || '').trim())[0] || firstGlyph(id);
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
