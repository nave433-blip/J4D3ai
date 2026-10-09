/** Dark theme tokens — Grok/Gemini-style chat app. */

export const theme = {
  bg: '#0a0a12',
  bgElevated: '#12121c',
  bgInput: '#1a1a28',
  border: '#26263a',
  text: '#f2f2f7',
  textDim: '#9a9ab0',
  accent: '#7c6cf5',
  accentDim: '#4a3fb5',
  userBubble: '#2b2b45',
  danger: '#ff6b6b',
  success: '#51d88a',
  radius: 14,
  fontMono: 'monospace',
} as const;

export type Theme = typeof theme;
